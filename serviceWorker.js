async function handleFetch(request) {
	// Perform the original fetch request and store the result in order to modify the response.
	try {
		var r = await fetch(request);
	}
	catch (e) {
		console.error(e)
	}
	if (r.status === 0) {
		return r;
	}
	// We add headers to the original response its headers, in order to enable cross-origin-isolation. And make it independent of the server config.
	const newHeaders = new Headers(r.headers);
	// COEP & COOP for cross-origin-isolation.
	newHeaders.set("Cross-Origin-Embedder-Policy", "require-corp");
	newHeaders.set("Cross-Origin-Opener-Policy", "same-origin");
	newHeaders.set("Cross-Origin-Resource-Policy", "cross-origin");
	/**
	 * This workaround is necessary due to a limitation of CheerpOS, which relies on the response URL being set to the resolved URL.
	 * When constructing a new response object, the URL is not set by the Response() constructor and the serviceworker respondwith() method will set the url to event.request.url in case of an empty string.
	 * To address this, we set the location URL to the resolved response URL and set the status code to 301 in the new Response object.
	 * This causes the request to bounce back to the serviceworker from Cheerpos, with the event.request.url now set to the resolved URL, which allows the respondWith method to properly set the response URL in our new response.
	 * https://developer.mozilla.org/en-US/docs/Web/API/FetchEvent/respondWith.
	*/
	if (r.redirected === true)
		newHeaders.set("location", r.url);
	// In case of a redirection, we set the status to 301, and body to null, in order to not transfer too much data needlessly
	const moddedResponse = new Response(r.redirected === true ? null : r.body, {
		headers: newHeaders,
		status: r.redirected === true ? 301 : r.status,
		statusText: r.statusText,
	});
	return moddedResponse;
}

// --------------------------------------------------------------------------
// WebVM proxy bridge
//
// The WebVM page is served over https, and the WebVM's HTTP proxy listens
// on plain http.  Chrome blocks an http:// subresource on an https:// page
// as mixed content *before* the request is made, so the page cannot reach
// the proxy itself.
//
// A service worker runs in its own context, so it can fetch the proxy's
// http:// address and hand the result back as a same-origin response.
// That keeps the page free of mixed content and lets the page inject the
// result with srcdoc, which keeps the DOM of the proxied page reachable.
//
// Two routes:
//   /webvm/httpsproxy?base=<origin>&target=<url>   -> the proxied document
//   /webvm/httpsproxy/raw?base=<origin>&path=<p>  -> one proxied resource
// --------------------------------------------------------------------------

const PROXY_BRIDGE_PREFIX = "/webvm/httpsproxy";

function bridgeParams(url) {
	return {
		base: (url.searchParams.get("base") || "").replace(/\/$/, ""),
		target: url.searchParams.get("target") || "",
		path: url.searchParams.get("path") || "",
	};
}

// Point the proxy's own URLs back at this bridge, so the sub-resources of
// the injected document are fetched same-origin instead of over http://.
function rewriteProxyUrls(html, base) {
	if (!base) return html;
	const replacement = PROXY_BRIDGE_PREFIX + "/raw?base=" + encodeURIComponent(base)
		+ "&path=/proxy/";
	return html.split(base + "/proxy/").join(replacement);
}

// The page is cross-origin isolated (COEP: require-corp), so every fetch
// the worker makes has to be CORS approved, and every response it hands
// back has to be embeddable.
const BRIDGE_RESPONSE_HEADERS = {
	"Access-Control-Allow-Origin": "*",
	"Cross-Origin-Resource-Policy": "cross-origin",
	"Cross-Origin-Embedder-Policy": "require-corp",
	"Cross-Origin-Opener-Policy": "same-origin",
};

// mode: "cors" is explicit on purpose: the default in a worker can fall
// back to a no-cors request, which a cross-origin isolated context
// rejects because the response carries no CORP header.
async function handleProxyBridge(url) {
	const params = bridgeParams(url);
	if (!params.base) {
		return new Response("missing base", { status: 400 });
	}
	const target = params.path
		? params.base + params.path
		: params.base + "/proxy/" + encodeURIComponent(params.target);
	try {
		const upstream = await fetch(target, {
			cache: "no-store",
			mode: "cors",
			credentials: "omit",
		});
		const text = await upstream.text();
		if (params.path) {
			return new Response(text, {
				status: upstream.status,
				headers: Object.assign({ "Content-Type": upstream.headers.get("Content-Type") || "application/octet-stream" }, BRIDGE_RESPONSE_HEADERS),
			});
		}
		return new Response(rewriteProxyUrls(text, params.base), {
			status: upstream.status,
			headers: Object.assign({ "Content-Type": "text/html; charset=utf-8" }, BRIDGE_RESPONSE_HEADERS),
		});
	} catch (err) {
		return new Response("proxy bridge failed: " + err, {
			status: 502,
			headers: BRIDGE_RESPONSE_HEADERS,
		});
	}
}

// A diagnostic route: reports what the service worker itself is able to
// fetch.  Used to tell a mixed-content block apart from an unreachable
// address.  /webvm/httpsproxy/diag?ip=<address>
async function handleDiag(url) {
	const ip = url.searchParams.get("ip") || "";
	const targets = [
		"http://" + ip + ":28082/health",
		"https://" + ip + ":28443/health",
		"http://" + ip + ":28082/proxy/" + encodeURIComponent("https://example.com/"),
		"https://example.com/",
		"http://example.com/",
	];
	const results = [];
	for (const target of targets) {
		const started = Date.now();
		for (const mode of ["cors", "no-cors"]) {
			try {
				const upstream = await fetch(target, {
					cache: "no-store",
					mode: mode,
					credentials: "omit",
				});
				const body = await upstream.text();
				results.push({
					target: target,
					mode: mode,
					ok: true,
					status: upstream.status,
					ms: Date.now() - started,
					body: body.slice(0, 120),
				});
				break;
			} catch (err) {
				results.push({
					target: target,
					mode: mode,
					ok: false,
					ms: Date.now() - started,
					name: err && err.name,
					error: String(err),
				});
			}
		}
	}
	return new Response(JSON.stringify(results, null, 2), {
		headers: Object.assign({ "Content-Type": "application/json" }, BRIDGE_RESPONSE_HEADERS),
	});
}

function serviceWorkerInit() {
	// Init the service worker.
	self.addEventListener("install", () => self.skipWaiting());
	self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
	// Listen for fetch requests and call handleFetch function.
	self.addEventListener("fetch", function (e) {
		try {
			const requestUrl = new URL(e.request.url);
			if (requestUrl.origin === self.location.origin
				&& requestUrl.pathname.startsWith(PROXY_BRIDGE_PREFIX)) {
				if (requestUrl.pathname.endsWith("/diag")) {
					e.respondWith(handleDiag(requestUrl));
				} else {
					e.respondWith(handleProxyBridge(requestUrl));
				}
				return;
			}
			e.respondWith(handleFetch(e.request));
		} catch (err) {
			console.log("Serviceworker NetworkError:" + err);
		}
	});
}

async function doRegister() {
	try {
		const registration = await navigator.serviceWorker.register(window.document.currentScript.src);
		console.log("Service Worker registered", registration.scope);
		// EventListener to make sure that the page gets reloaded when a new serviceworker gets installed.
		// f.e on first access.
		registration.addEventListener("updatefound", () => {
			console.log("Reloading the page to transfer control to the Service Worker.");
			try {
				window.location.reload();
			} catch (err) {
				console.log("Service Worker failed reloading the page. ERROR:" + err);
			};
		});
		// When the registration is active, but it's not controlling the page, we reload the page to have it take control.
		// This f.e occurs when you hard-reload (shift + refresh). https://www.w3.org/TR/service-workers/#navigator-service-worker-controller
		if (registration.active && !navigator.serviceWorker.controller) {
			console.log("Reloading the page to transfer control to the Service Worker.");
			try {
				window.location.reload();
			} catch (err) {
				console.log("Service Worker failed reloading the page. ERROR:" + err);
			};
		}
	}
	catch {
		console.error("Service Worker failed to register:", e)
	}
}

async function serviceWorkerRegister() {
	if (window.crossOriginIsolated) return;
	if (!window.isSecureContext) {
		console.log("Service Worker not registered, a secure context is required.");
		return;
	}
	// Register the service worker and reload the page to transfer control to the serviceworker.
	if ("serviceWorker" in navigator)
		await doRegister();
	else
		console.log("Service worker is not supported in this browser");
}

if (typeof window === 'undefined') // If the script is running in a Service Worker context
	serviceWorkerInit()
else // If the script is running in the browser context
	serviceWorkerRegister();
