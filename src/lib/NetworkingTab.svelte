<script>
	import { networkData, startLogin, updateButtonData } from '$lib/network.js'
	import { createEventDispatcher } from 'svelte';
	import PanelButton from './PanelButton.svelte';
	var dispatch = createEventDispatcher();
	var connectionState = networkData.connectionState;
	var exitNode = networkData.exitNode;
	var kaggleTcpLink = networkData.kaggleTcpLink;

	function handleConnect() {
		connectionState.set("DOWNLOADING");
		dispatch('connect');
	}

	let buttonData = null;
	$: buttonData = updateButtonData($connectionState, handleConnect);
</script>
<h1 class="text-lg font-bold">Networking</h1>
<PanelButton buttonImage="assets/tailscale.svg" clickUrl={buttonData.clickUrl} clickHandler={buttonData.clickHandler} rightClickHandler={buttonData.rightClickHandler} buttonTooltip={buttonData.buttonTooltip} buttonText={buttonData.buttonText}>
	{#if $connectionState == "CONNECTED"}
		<i class='fas fa-circle fa-xs ml-auto {$exitNode ? 'text-green-500' : 'text-amber-500'}' title={$exitNode ? 'Ready' : 'No exit node'}></i>
	{/if}
</PanelButton>
<p>WebVM can connect to other devices over Tailscale.</p>
<p>Using Tailscale is required since browsers do not expose raw TCP/UDP sockets.</p>
{#if $kaggleTcpLink}
	<div class="mt-3 rounded border border-green-500/40 p-2 text-sm">
		<p class="font-semibold">Kaggle TCP bridge online</p>
		<p>Peer: {$kaggleTcpLink.ip}:{$kaggleTcpLink.port}</p>
		<p class="break-all">SOCKS5: socks5h://{$kaggleTcpLink.ip}:{$kaggleTcpLink.port}</p>
		<p class="break-all">HTTP/HTTPS proxy: http://{$kaggleTcpLink.ip}:{$kaggleTcpLink.port}</p>
		<p class="mt-1 text-xs">New shell prompts load the proxy settings from /data/kaggle-tcp-link.env.</p>
	</div>
{/if}
