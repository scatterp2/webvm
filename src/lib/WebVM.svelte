<script>
	import { onMount, tick } from 'svelte';
	import { get } from 'svelte/store';
	import Nav from 'labs/packages/global-navbar/src/Nav.svelte';
	import SideBar from '$lib/SideBar.svelte';
	import '$lib/global.css';
	import '@xterm/xterm/css/xterm.css'
	import '@fortawesome/fontawesome-free/css/all.min.css'
	import { networkInterface, networkData, currentIpStore, currentDnsNameStore, startLogin, attachDataDevice } from '$lib/network.js'
	import { cpuActivity, diskActivity, cpuPercentage, diskLatency } from '$lib/activities.js'
	import { introMessage, errorMessage, unexpectedErrorMessage } from '$lib/messages.js'
	import { displayConfig, handleToolImpl } from '$lib/anthropic.js'
	import { tryPlausible } from '$lib/plausible.js'

	export let configObj = null;
	export let processCallback = null;
	export let cacheId = null;
	export let cpuActivityEvents = [];
	export let diskLatencies = [];
	export let activityEventsInterval = 0;

	var term = null;
	var cx = null;
	var fitAddon = null;
	var cxReadFunc = null;
	var blockCache = null;
	var processCount = 0;
	var curVT = 0;
	var sideBarPinned = false;

	// --- Proxied page panel ---------------------------------------------
	// The panel shows a page fetched through the WebVM's own HTTP proxy.
	//
	// This page is served over https and the proxy listens on plain http,
	// so the page cannot fetch the proxy directly: Chrome blocks an http://
	// subresource here as mixed content, before the request is even made.
	// The site's service worker bridges the gap — it fetches the proxy's
	// http:// address from its own context and hands the page a same-origin
	// response, which the page injects with srcdoc.  A srcdoc frame
	// inherits this page's origin, so the DOM of the proxied page stays
	// reachable.
	var proxyPanelOpen = false;
	var proxyTarget = "https://www.kaggle.com/";
	var proxyStatus = "";
	var proxyFrameEl = null;
	const PROXY_TLS_PORT = 28443;
	// A Tailscale Serve endpoint publishes the proxy with a publicly trusted
	// certificate, so the service worker can fetch it over https without a
	// development CA having to be trusted.  Serve has to be enabled on the
	// tailnet first (see the README).
	const TAILSCALE_SERVE_ORIGIN = "https://desktop-21m8c49.tailf987e.ts.net";

	// The WebVM's plain-http proxy origin, taken from the netmap because
	// the address changes on every boot.
	$: proxyHttpBase = $currentIpStore
		? ("http://" + $currentIpStore + ":28082")
		: "";

	// Bases the proxy is reachable at, in the order they are tried.
	//
	// An https origin is fetched directly by the page with CORS: the proxy
	// sends Access-Control-Allow-Origin, so the response is readable and
	// can be injected with srcdoc, which keeps DOM access.
	//
	// A plain-http origin cannot be fetched by the page (mixed content), so
	// it goes through the site's service worker, which runs in the browser
	// too and is bound by the same rule.  It only works for origins that
	// are "potentially trustworthy" (127.0.0.1) and that the browser is
	// allowed to reach.
	function proxyBases()
	{
		const https = [];
		const http = [];
		let override = null;
		if(typeof window !== "undefined")
		{
			const params = new URLSearchParams("?" + window.location.hash.substr(1));
			override = params.get("proxyOrigin");
		}
		if(override)
			https.push(override.replace(/\/$/, ""));
		if($currentDnsNameStore)
			https.push("https://" + $currentDnsNameStore + ":" + PROXY_TLS_PORT);
		if(TAILSCALE_SERVE_ORIGIN)
			https.push(TAILSCALE_SERVE_ORIGIN);
		if(proxyHttpBase)
			http.push(proxyHttpBase);
		return {https: https, http: http};
	}

	async function loadProxyFrame()
	{
		if(!proxyHttpBase) {
			proxyStatus = "Waiting for the WebVM's Tailscale address...";
			return;
		}
		proxyStatus = "Opening the proxy bridge for " + proxyTarget + "...";
		// The page is https and the proxy is http, so the page cannot fetch
		// it itself.  A popup opened on the proxy's own http origin can:
		// it asks the proxy for a self-contained document (every
		// sub-resource is inlined server side, where the mixed-content
		// rule does not apply) and posts it back here.  Injecting it with
		// srcdoc keeps the frame same-origin with this page, so the DOM
		// of the proxied page stays reachable.
		try {
			const popup = window.open(
				proxyHttpBase + "/?bridge=1&target="
					+ encodeURIComponent(proxyTarget),
				"webvm-proxy-bridge", "width=900,height=700");
			if(!popup) {
				proxyStatus = "The browser blocked the popup - allow popups "
					+ "for this site and try again.";
				return;
			}
			proxyStatus = "Waiting for the bridge popup to return the "
				+ proxyTarget + " document...";
		} catch (e) {
			proxyStatus = "Could not open the bridge popup (" + e + ").";
		}
	}

	function handleProxyMessage(event)
	{
		const data = event.data;
		if(!data || typeof data !== "object") return;
		if(data.type === "webvm-proxy-html") {
			if(proxyFrameEl) {
				proxyFrameEl.srcdoc = data.html;
				proxyStatus = "Loaded " + data.html.length
					+ " bytes through the bridge - srcdoc frame keeps DOM access.";
			}
		} else if(data.type === "webvm-proxy-error") {
			proxyStatus = "Bridge failed (" + data.error + ").";
		}
	}

	function toggleProxyPanel()
	{
		proxyPanelOpen = !proxyPanelOpen;
		if(proxyPanelOpen) {
			setTimeout(() => loadProxyFrame(), 0);
		}
	}

	function setProxyTarget(url)
	{
		proxyTarget = url;
		if(proxyPanelOpen) {
			loadProxyFrame();
		}
	}

	function writeData(buf, vt)
	{
		if(vt != 1)
			return;
		term.write(new Uint8Array(buf));
	}
	function readData(str)
	{
		if(cxReadFunc == null)
			return;
		for(var i=0;i<str.length;i++)
			cxReadFunc(str.charCodeAt(i));
	}
	function printMessage(msg)
	{
		for(var i=0;i<msg.length;i++)
			term.write(msg[i] + "\n");
	}
	function expireEvents(list, curTime, limitTime)
	{
		while(list.length > 1)
		{
			if(list[1].t < limitTime)
			{
				list.shift();
			}
			else
			{
				break;
			}
		}
	}
	function cleanupEvents()
	{
		var curTime = Date.now();
		var limitTime = curTime - 10000;
		expireEvents(cpuActivityEvents, curTime, limitTime);
		computeCpuActivity(curTime, limitTime);
		if(cpuActivityEvents.length == 0)
		{
			clearInterval(activityEventsInterval);
			activityEventsInterval = 0;
		}
	}
	function computeCpuActivity(curTime, limitTime)
	{
		var totalActiveTime = 0;
		var lastActiveTime = limitTime;
		var lastWasActive = false;
		for(var i=0;i<cpuActivityEvents.length;i++)
		{
			var e = cpuActivityEvents[i];
			// NOTE: The first event could be before the limit,
			//       we need at least one event to correctly mark
			//       active time when there is long time under load
			var eTime = e.t;
			if(eTime < limitTime)
				eTime = limitTime;
			if(e.state == "ready")
			{
				// Inactive state, add the time from lastActiveTime
				totalActiveTime += (eTime - lastActiveTime);
				lastWasActive = false;
			}
			else
			{
				// Active state
				lastActiveTime = eTime;
				lastWasActive = true;
			}
		}
		// Add the last interval if needed
		if(lastWasActive)
		{
			totalActiveTime += (curTime - lastActiveTime);
		}
		cpuPercentage.set(Math.ceil((totalActiveTime / 10000) * 100));
	}
	function hddCallback(state)
	{
		diskActivity.set(state != "ready");
	}
	function latencyCallback(latency)
	{
		diskLatencies.push(latency);
		if(diskLatencies.length > 30)
			diskLatencies.shift();
		// Average the latency over at most 30 blocks
		var total = 0;
		for(var i=0;i<diskLatencies.length;i++)
			total += diskLatencies[i];
		var avg = total / diskLatencies.length;
		diskLatency.set(Math.ceil(avg));
	}
	function cpuCallback(state)
	{
		cpuActivity.set(state != "ready");
		var curTime = Date.now();
		var limitTime = curTime - 10000;
		expireEvents(cpuActivityEvents, curTime, limitTime);
		cpuActivityEvents.push({t: curTime, state: state});
		computeCpuActivity(curTime, limitTime);
		// Start an interval timer to cleanup old samples when no further activity is received
		if(activityEventsInterval != 0)
			clearInterval(activityEventsInterval);
		activityEventsInterval = setInterval(cleanupEvents, 2000);
	}
	function computeXTermFontSize()
	{
		return parseInt(getComputedStyle(document.body).fontSize);
	}
	function setScreenSize(display)
	{
		var internalMult = 1.0;
		var displayWidth = display.offsetWidth;
		var displayHeight = display.offsetHeight;
		var minWidth = 1024;
		var minHeight = 768;
		if(displayWidth < minWidth)
			internalMult = minWidth / displayWidth;
		if(displayHeight < minHeight)
			internalMult = Math.max(internalMult, minHeight / displayHeight);
		var internalWidth = Math.floor(displayWidth * internalMult);
		var internalHeight = Math.floor(displayHeight * internalMult);
		cx.setKmsCanvas(display, internalWidth, internalHeight);
		// Compute the size to be used for AI screenshots
		var screenshotMult = 1.0;
		var maxWidth = 1024;
		var maxHeight = 768;
		if(internalWidth > maxWidth)
			screenshotMult = maxWidth / internalWidth;
		if(internalHeight > maxHeight)
			screenshotMult = Math.min(screenshotMult, maxHeight / internalHeight);
		var screenshotWidth = Math.floor(internalWidth * screenshotMult);
		var screenshotHeight = Math.floor(internalHeight * screenshotMult);
		// Track the state of the mouse as requested by the AI, to avoid losing the position due to user movement
		displayConfig.set({width: screenshotWidth, height: screenshotHeight, mouseMult: internalMult * screenshotMult});
	}
	var curInnerWidth = 0;
	var curInnerHeight = 0;
	function handleResize()
	{
		// Avoid spurious resize events caused by the soft keyboard
		if(curInnerWidth == window.innerWidth && curInnerHeight == window.innerHeight)
			return;
		curInnerWidth = window.innerWidth;
		curInnerHeight = window.innerHeight;
		triggerResize();
	}
	function triggerResize()
	{
		term.options.fontSize = computeXTermFontSize();
		fitAddon.fit();
		const display = document.getElementById("display");
		if(display)
			setScreenSize(display);
	}
	async function initTerminal()
	{
		const { Terminal } = await import('@xterm/xterm');
		const { FitAddon } = await import('@xterm/addon-fit');
		const { WebLinksAddon } = await import('@xterm/addon-web-links');
		term = new Terminal({cursorBlink:true, convertEol:true, fontFamily:"monospace", fontWeight: 400, fontWeightBold: 700, fontSize: computeXTermFontSize()});
		fitAddon = new FitAddon();
		term.loadAddon(fitAddon);
		var linkAddon = new WebLinksAddon();
		term.loadAddon(linkAddon);
		const consoleDiv = document.getElementById("console");
		term.open(consoleDiv);
		term.scrollToTop();
		fitAddon.fit();
		window.addEventListener("resize", handleResize);
		term.focus();
		term.onData(readData);
		// Avoid undesired default DnD handling
		function preventDefaults (e) {
			e.preventDefault()
			e.stopPropagation()
		}
		consoleDiv.addEventListener("dragover", preventDefaults, false);
		consoleDiv.addEventListener("dragenter", preventDefaults, false);
		consoleDiv.addEventListener("dragleave", preventDefaults, false);
		consoleDiv.addEventListener("drop", preventDefaults, false);
		curInnerWidth = window.innerWidth;
		curInnerHeight = window.innerHeight;
		if(configObj.printIntro)
			printMessage(introMessage);
		try
		{
			await initCheerpX();
		}
		catch(e)
		{
			printMessage(unexpectedErrorMessage);
			printMessage([e.toString()]);
			return;
		}
	}
	function handleActivateConsole(vt)
	{
		if(curVT == vt)
			return;
		curVT = vt;
		if(vt != 7)
			return;
		// Raise the display to the foreground
		const display = document.getElementById("display");
		display.parentElement.style.zIndex = 5;
		tryPlausible("Display activated");
	}
	function handleProcessCreated()
	{
		processCount++;
		if(processCallback)
			processCallback(processCount);
	}
	async function initCheerpX()
	{
		const CheerpX = await import('@leaningtech/cheerpx');
		var blockDevice = null;
		switch(configObj.diskImageType)
		{
			case "cloud":
				try
				{
					blockDevice = await CheerpX.CloudDevice.create(configObj.diskImageUrl);
				}
				catch(e)
				{
					// Report the failure and try again with plain HTTP
					var wssProtocol = "wss:";
					if(configObj.diskImageUrl.startsWith(wssProtocol))
					{
						// WebSocket protocol failed, try agin using plain HTTP
						tryPlausible("WS Disk failure");
						blockDevice = await CheerpX.CloudDevice.create("https:" + configObj.diskImageUrl.substr(wssProtocol.length));
					}
					else
					{
						// No other recovery option
						throw e;
					}
				}
				break;
			case "bytes":
				blockDevice = await CheerpX.HttpBytesDevice.create(configObj.diskImageUrl);
				break;
			case "github":
				blockDevice = await CheerpX.GitHubDevice.create(configObj.diskImageUrl);
				break;
			default:
				throw new Error("Unrecognized device type");
		}
		blockCache = await CheerpX.IDBDevice.create(cacheId);
		var overlayDevice = await CheerpX.OverlayDevice.create(blockDevice, blockCache);
		var webDevice = await CheerpX.WebDevice.create("");
		var documentsDevice = await CheerpX.WebDevice.create("documents");
		var dataDevice = await CheerpX.DataDevice.create();
		attachDataDevice(dataDevice);
		var mountPoints = [
			// The root filesystem, as an Ext2 image
			{type:"ext2", dev:overlayDevice, path:"/"},
			// Access to files on the Web server, relative to the current page
			{type:"dir", dev:webDevice, path:"/web"},
			// Access to read-only data coming from JavaScript
			{type:"dir", dev:dataDevice, path:"/data"},
			// Automatically created device files
			{type:"devs", path:"/dev"},
			// Pseudo-terminals
			{type:"devpts", path:"/dev/pts"},
			// The Linux 'proc' filesystem which provides information about running processes
			{type:"proc", path:"/proc"},
			// The Linux 'sysfs' filesystem which is used to enumerate emulated devices
			{type:"sys", path:"/sys"},
			// Convenient access to sample documents in the user directory
			{type:"dir", dev:documentsDevice, path:"/home/user/documents"}
		];
		try
		{
			cx = await CheerpX.Linux.create({mounts: mountPoints, networkInterface: networkInterface});
		}
		catch(e)
		{
			printMessage(errorMessage);
			printMessage([e.toString()]);
			return;
		}
		cx.registerCallback("cpuActivity", cpuCallback);
		cx.registerCallback("diskActivity", hddCallback);
		cx.registerCallback("diskLatency", latencyCallback);
		cx.registerCallback("processCreated", handleProcessCreated);
		term.scrollToBottom();
		cxReadFunc = cx.setCustomConsole(writeData, term.cols, term.rows);
		const display = document.getElementById("display");
		if(display)
		{
			setScreenSize(display);
			cx.setActivateConsole(handleActivateConsole);
		}
		// Reset requests are serviced only after the virtual disk and Linux
		// runtime are initialized, so blockCache.reset() is never skipped.
		runtimeReady = true;
		if (pendingResetGeneration) {
			await performGenerationReset();
			return;
		}
		// Run the command in a loop, in case the user exits
		while (true)
		{
			await cx.run(configObj.cmd, configObj.args, configObj.opts);
		}
	}
	let pendingResetGeneration = null;
	let runtimeReady = false;
	let resetInProgress = false;

	async function performGenerationReset() {
		if (!runtimeReady || !blockCache || !pendingResetGeneration || resetInProgress)
			return;
		resetInProgress = true;
		const generation = pendingResetGeneration;
		pendingResetGeneration = null;
		console.info('[WebVM reset] Resetting disk cache for generation ' + generation);
		try {
			await blockCache.reset();
			console.info('[WebVM reset] Disk cache reset complete; reloading WebVM.');
			location.reload();
		} catch (error) {
			resetInProgress = false;
			// The page-head trigger records a generation before dispatching it to
			// prevent reload loops. If the cache reset fails, release that marker
			// so the next 15-second poll can retry this same generation.
			try {
				const handledKey = 'webvm-reset-last-source-version';
				const handledVersion = localStorage.getItem(handledKey);
				if (handledVersion === 'value:' + generation || handledVersion === 'modified:' + generation) {
					localStorage.removeItem(handledKey);
				}
			} catch (markerError) {
				console.warn('[WebVM reset] Could not release the handled marker for retry.', markerError);
			}
			console.error('[WebVM reset] Disk cache reset failed; the trigger will be retried on the next poll.', error);
		}
	}

	function handleGenerationReset(event) {
		const generation = event?.detail?.generation || window.__webvmPendingResetGeneration;
		if (!generation)
			return;
		pendingResetGeneration = String(generation);
		void performGenerationReset();
	}

	onMount(() => {
		window.addEventListener('webvm-reset-request', handleGenerationReset);
		// The proxy bridge popup posts the self-contained document back here.
		window.addEventListener('message', handleProxyMessage);
		// app.html runs in the document head, before this component mounts.
		if (window.__webvmPendingResetGeneration) {
			pendingResetGeneration = String(window.__webvmPendingResetGeneration);
		}
		void initTerminal();
		return () => {
			window.removeEventListener('webvm-reset-request', handleGenerationReset);
			window.removeEventListener('message', handleProxyMessage);
		};
	});

	// Auto-connect using the auth key supplied through networkInterface by the page URL hash.
	// Aligned with upstream leaningtech/webvm PR #235: when an auth key is set,
	// skip the login popup and call networkLogin() directly.
	let autoConnectAttempted = false;

	async function tryAutoConnect() {
		if(autoConnectAttempted || !cx) return;
		autoConnectAttempted = true;
		if(!networkInterface.authKey) return;

		// Let the CheerpX runtime loop (cx.run) start before triggering
		// Tailscale, so the cooperative scheduler can process the login.
		await new Promise(resolve => setTimeout(resolve, 2000));
		networkData.connectionState.set("LOGINSTARTING");
		cx.networkLogin();
	}

	// Watch for cx to become available and trigger auto-connect
	$: if(cx && !autoConnectAttempted) {
		tryAutoConnect();
	}

	// Print this node's Tailscale address into the terminal as soon as the
	// netmap reports it.  The address changes on every boot, so it must
	// never be hard-coded anywhere.
	let printedTailscaleIp = "";
	$: {
		const ip = $currentIpStore;
		if (ip && ip !== printedTailscaleIp && term) {
			printedTailscaleIp = ip;
			term.write(
				"\r\n" +
				"====================================================================\r\n" +
				"TAILSCALE_IP " + ip + "\r\n" +
				"TELNET   plink.exe -raw -P 2323 " + ip + "\r\n" +
				"PROXY    http://" + ip + ":28082/\r\n" +
				"FORWARD  http://" + ip + ":28081/\r\n" +
				"====================================================================\r\n"
			);
		}
	}


	async function handleConnect()
	{
		if(networkInterface.authKey)
		{
			networkData.connectionState.set("LOGINSTARTING");
			cx.networkLogin();
			return;
		}
		const w = window.open("login.html", "_blank");
		cx.networkLogin();
		try
		{
			w.location.href = await startLogin();
		}
		catch(e)
		{
			w.close();
			console.warn(e);
		}
	}
	async function handleReset()
	{
		// Be robust before initialization
		if(blockCache == null)
			return;
		await blockCache.reset();
		location.reload();
	}
	async function handleTool(tool)
	{
		return await handleToolImpl(tool, term);
	}
	async function handleSidebarPinChange(event)
	{
		sideBarPinned = event.detail;
		// Make sure the pinning state of reflected in the layout
		await tick();
		// Adjust the layout based on the new sidebar state
		triggerResize();
	}
</script>

<main class="relative w-full h-full">
	<Nav />
	<div class="absolute top-10 bottom-0 left-0 right-0">
		<SideBar on:connect={handleConnect} on:reset={handleReset} handleTool={!configObj.needsDisplay || curVT == 7 ? handleTool : null} on:sidebarPinChange={handleSidebarPinChange}>
			<slot></slot>
		</SideBar>
		{#if configObj.needsDisplay}
			<div class="absolute top-0 bottom-0 {sideBarPinned ? 'left-[23.5rem]' : 'left-14'} right-0">
				<canvas class="w-full h-full cursor-none" id="display"></canvas>
			</div>
		{/if}
		<div class="absolute top-0 bottom-0 {sideBarPinned ? 'left-[23.5rem]' : 'left-14'} {proxyPanelOpen ? 'right-1/2' : 'right-0'} p-1 scrollbar" id="console">
		</div>
		{#if proxyPanelOpen}
			<div class="absolute top-0 bottom-0 right-0 w-1/2 flex flex-col bg-gray-900 border-l border-gray-700">
				<div class="flex items-center gap-2 px-2 py-1 bg-gray-800 text-xs text-gray-200">
					<strong>Proxy</strong>
					<span class="truncate opacity-70">{proxyTarget}</span>
					<span class="flex-1"></span>
					<button class="px-2 py-0.5 rounded bg-gray-700 hover:bg-gray-600" on:click={() => setProxyTarget("https://www.kaggle.com/")}>Kaggle</button>
					<button class="px-2 py-0.5 rounded bg-gray-700 hover:bg-gray-600" on:click={() => setProxyTarget("https://example.com/")}>Example</button>
					<button class="px-2 py-0.5 rounded bg-gray-700 hover:bg-gray-600" on:click={() => setProxyTarget("https://www.google.com/")}>Google</button>
					<button class="px-2 py-0.5 rounded bg-gray-700 hover:bg-gray-600" on:click={toggleProxyPanel}>Close</button>
				</div>
				<div class="px-2 py-1 bg-gray-900 text-xs text-gray-400 border-b border-gray-800">{proxyStatus}</div>
				<iframe
					bind:this={proxyFrameEl}
					title="Proxied page through the WebVM proxy"
					class="flex-1 w-full bg-white"></iframe>
			</div>
		{/if}
		{#if !proxyPanelOpen}
			<button
				class="absolute bottom-2 right-2 z-20 px-3 py-1.5 rounded bg-gray-800 border border-gray-600 text-xs text-gray-100 hover:bg-gray-700"
				on:click={toggleProxyPanel}
				title="Show the page proxied through the WebVM">
				Proxy &#9638;
			</button>
		{/if}
	</div>
</main>
