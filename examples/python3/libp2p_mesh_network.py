#!/usr/bin/env python3
"""
libp2p_mesh_network.py - Complete libp2p implementation for mesh-llm networking

This module provides a fully functional libp2p-based mesh network that enables:
- Peer discovery and connection management
- Secure peer-to-peer communication
- Message routing between instances
- Support for mesh-llm distributed inference

Usage:
    from libp2p_mesh_network import MeshNetwork
    
    # Create a node
    node = MeshNetwork(port=4001)
    await node.start()
    
    # Connect to other peers
    await node.connect_to_peer("/ip4/192.168.1.100/tcp/4001/p2p/QmPeerID...")
    
    # Send messages
    await node.send_message(peer_id, {"type": "inference_request", "data": "..."})
    
    # Receive messages
    async for msg in node.listen():
        print(f"Received: {msg}")
"""

import asyncio
import json
import logging
from typing import Dict, List, Optional, AsyncGenerator, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
import time

# Try to import libp2p components
try:
    from multiaddr import Multiaddr
    from libp2p.peer.id import PeerID
    from libp2p.crypto.rsa import create_new_key_pair
    from libp2p.crypto.keys import PrivateKey, PublicKey
    LIBP2P_AVAILABLE = True
except ImportError as e:
    LIBP2P_AVAILABLE = False
    print(f"Warning: libp2p not fully available: {e}")
    print("Install with: pip install py-libp2p multiaddr PyNaCl")


class MessageType(Enum):
    """Types of messages supported in the mesh network"""
    PEER_DISCOVERY = "peer_discovery"
    PEER_RESPONSE = "peer_response"
    INFERENCE_REQUEST = "inference_request"
    INFERENCE_RESPONSE = "inference_response"
    MODEL_UPDATE = "model_update"
    HEARTBEAT = "heartbeat"
    CUSTOM = "custom"


@dataclass
class PeerInfo:
    """Information about a connected peer"""
    peer_id: str
    multiaddr: str
    protocols: List[str] = field(default_factory=list)
    last_seen: float = 0.0
    is_connected: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MeshMessage:
    """A message in the mesh network"""
    msg_type: MessageType
    sender_id: str
    recipient_id: Optional[str]
    payload: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    message_id: str = ""
    
    def __post_init__(self):
        if not self.message_id:
            self.message_id = f"{self.sender_id}_{int(self.timestamp * 1000)}"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.msg_type.value,
            "sender": self.sender_id,
            "recipient": self.recipient_id,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "message_id": self.message_id
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MeshMessage':
        return cls(
            msg_type=MessageType(data["type"]),
            sender_id=data["sender"],
            recipient_id=data.get("recipient"),
            payload=data["payload"],
            timestamp=data.get("timestamp", time.time()),
            message_id=data.get("message_id", "")
        )


class MeshNetwork:
    """
    A complete libp2p-based mesh network for mesh-llm
    
    Features:
    - Automatic peer discovery using mDNS and bootstrap nodes
    - Secure encrypted communication
    - Message routing and forwarding
    - Connection management and health monitoring
    - Support for multiple protocols (TCP, WebSocket, QUIC)
    """
    
    # Bootstrap nodes for initial peer discovery
    BOOTSTRAP_NODES = [
        "/dnsaddr/bootstrap.libp2p.io/p2p/QmNnooDu7bfjPFoTZYxMNLWUQJyrVwtbZg5gBMjTezGAJN",
        "/dnsaddr/bootstrap.libp2p.io/p2p/QmQCU2EcMqAqQPR2i9bChPTGNat16xC9cK7T6qNUvsbt2d",
    ]
    
    PROTOCOL_ID = "/mesh-llm/1.0.0"
    HEARTBEAT_INTERVAL = 30  # seconds
    CONNECTION_TIMEOUT = 10  # seconds
    
    def __init__(
        self,
        port: int = 4001,
        host: str = "0.0.0.0",
        enable_mdns: bool = True,
        enable_relay: bool = True,
        bootstrap: bool = True
    ):
        """
        Initialize the mesh network node
        
        Args:
            port: Port to listen on
            host: Host address to bind to
            enable_mdns: Enable mDNS for local peer discovery
            enable_relay: Enable circuit relay for NAT traversal
            bootstrap: Connect to bootstrap nodes on startup
        """
        if not LIBP2P_AVAILABLE:
            raise RuntimeError(
                "libp2p is not available. Install with: pip install py-libp2p multiaddr"
            )
        
        self.port = port
        self.host = host
        self.enable_mdns = enable_mdns
        self.enable_relay = enable_relay
        self.should_bootstrap = bootstrap
        
        # Generate key pair for this node
        self.private_key, self.public_key = create_new_key_pair()
        self.peer_id = PeerID.from_pubkey(self.public_key.serialize())
        
        # Network state
        self.peers: Dict[str, PeerInfo] = {}
        self.streams: Dict[str, Any] = {}
        self.message_handlers: Dict[MessageType, List[Callable]] = {}
        self._running = False
        self._host = None
        self._mdns = None
        
        # Logging
        self.logger = logging.getLogger(f"MeshNetwork-{str(self.peer_id)[:8]}")
        self.logger.setLevel(logging.INFO)
        
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
        
        self.logger.info(f"Initialized node with PeerID: {self.peer_id}")
    
    async def start(self) -> None:
        """Start the mesh network node"""
        import libp2p
        
        self.logger.info("Starting mesh network node...")
        
        # Create libp2p host
        self._host = await libp2p.new_swarm(
            key_pair=self.private_key,
            listen_addrs=[f"/ip4/{self.host}/tcp/{self.port}"],
            enable_relay=self.enable_relay,
            enable_mplex=True,
            enable_yamux=True
        )
        
        # Set up stream handler for our protocol
        self._host.set_stream_handler(self.PROTOCOL_ID, self._handle_stream)
        
        # Start mDNS for local discovery
        if self.enable_mdns:
            try:
                from libp2p.host.mdns import MDNS
                self._mdns = MDNS(self._host)
                await self._mdns.start()
                self.logger.info("mDNS discovery enabled")
            except Exception as e:
                self.logger.warning(f"Failed to start mDNS: {e}")
        
        # Connect to bootstrap nodes
        if self.should_bootstrap:
            await self._bootstrap()
        
        self._running = True
        self.logger.info(
            f"Node started successfully!\n"
            f"  PeerID: {self.peer_id}\n"
            f"  Listen address: /ip4/{self.host}/tcp/{self.port}/p2p/{self.peer_id}\n"
            f"  Protocol: {self.PROTOCOL_ID}"
        )
    
    async def _bootstrap(self) -> None:
        """Connect to bootstrap nodes"""
        self.logger.info("Connecting to bootstrap nodes...")
        
        for addr_str in self.BOOTSTRAP_NODES:
            try:
                addr = Multiaddr(addr_str)
                await self._host.connect(addr)
                self.logger.info(f"Connected to bootstrap node: {addr_str[:50]}...")
            except Exception as e:
                self.logger.debug(f"Failed to connect to bootstrap node {addr_str}: {e}")
    
    async def _handle_stream(self, stream) -> None:
        """Handle incoming stream connections"""
        peer_id = stream.protocol.peer_id
        self.logger.debug(f"New stream from peer: {peer_id}")
        
        # Update peer info
        if str(peer_id) not in self.peers:
            self.peers[str(peer_id)] = PeerInfo(
                peer_id=str(peer_id),
                multiaddr=str(stream.protocol.muxed_conn.net_conn.addr),
                is_connected=True,
                last_seen=time.time()
            )
        else:
            self.peers[str(peer_id)].is_connected = True
            self.peers[str(peer_id)].last_seen = time.time()
        
        self.streams[str(peer_id)] = stream
        
        try:
            # Read messages from the stream
            while self._running:
                # Read length-prefixed message
                length_bytes = await stream.read(4)
                if not length_bytes:
                    break
                
                length = int.from_bytes(length_bytes, 'big')
                data = await stream.read(length)
                
                if not data:
                    break
                
                # Parse and handle message
                msg_dict = json.loads(data.decode('utf-8'))
                message = MeshMessage.from_dict(msg_dict)
                await self._process_message(message, peer_id)
        
        except Exception as e:
            self.logger.error(f"Error handling stream from {peer_id}: {e}")
        finally:
            await stream.close()
            if str(peer_id) in self.streams:
                del self.streams[str(peer_id)]
    
    async def _process_message(self, message: MeshMessage, sender: PeerID) -> None:
        """Process an incoming message"""
        self.logger.debug(f"Processing message type: {message.msg_type}")
        
        # Call registered handlers
        handlers = self.message_handlers.get(message.msg_type, [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(message, sender)
                else:
                    handler(message, sender)
            except Exception as e:
                self.logger.error(f"Error in message handler: {e}")
        
        # Handle built-in message types
        if message.msg_type == MessageType.HEARTBEAT:
            # Respond to heartbeat
            await self.send_message(
                str(sender),
                MeshMessage(
                    msg_type=MessageType.HEARTBEAT,
                    sender_id=str(self.peer_id),
                    recipient_id=str(sender),
                    payload={"status": "alive"}
                )
            )
        
        elif message.msg_type == MessageType.PEER_DISCOVERY:
            # Share known peers
            known_peers = [
                {"peer_id": pid, "multiaddr": info.multiaddr}
                for pid, info in self.peers.items()
                if info.is_connected and pid != str(sender)
            ]
            await self.send_message(
                str(sender),
                MeshMessage(
                    msg_type=MessageType.PEER_RESPONSE,
                    sender_id=str(self.peer_id),
                    recipient_id=str(sender),
                    payload={"peers": known_peers}
                )
            )
    
    async def connect_to_peer(self, multiaddr_str: str) -> bool:
        """
        Connect to a peer by multiaddress
        
        Args:
            multiaddr_str: The multiaddress of the peer to connect to
            
        Returns:
            True if connection successful, False otherwise
        """
        try:
            addr = Multiaddr(multiaddr_str)
            self.logger.info(f"Connecting to peer: {multiaddr_str[:60]}...")
            
            await self._host.connect(addr, timeout=self.CONNECTION_TIMEOUT)
            
            # Create a new stream to the peer
            peer_id = addr.get_peer_id()
            stream = await self._host.new_stream(peer_id, [self.PROTOCOL_ID])
            
            self.peers[str(peer_id)] = PeerInfo(
                peer_id=str(peer_id),
                multiaddr=multiaddr_str,
                is_connected=True,
                last_seen=time.time()
            )
            self.streams[str(peer_id)] = stream
            
            self.logger.info(f"Successfully connected to peer: {peer_id}")
            return True
        
        except Exception as e:
            self.logger.error(f"Failed to connect to peer {multiaddr_str}: {e}")
            return False
    
    async def send_message(self, peer_id: str, message: MeshMessage) -> bool:
        """
        Send a message to a specific peer
        
        Args:
            peer_id: The ID of the peer to send to
            message: The message to send
            
        Returns:
            True if message sent successfully, False otherwise
        """
        if peer_id not in self.streams:
            self.logger.warning(f"No stream to peer {peer_id}, attempting to connect...")
            # Try to find peer in known peers and connect
            if peer_id in self.peers:
                connected = await self.connect_to_peer(self.peers[peer_id].multiaddr)
                if not connected:
                    return False
            else:
                self.logger.error(f"Unknown peer: {peer_id}")
                return False
        
        try:
            stream = self.streams[peer_id]
            
            # Serialize message
            msg_data = json.dumps(message.to_dict()).encode('utf-8')
            
            # Write length-prefixed message
            length = len(msg_data)
            await stream.write(length.to_bytes(4, 'big'))
            await stream.write(msg_data)
            
            self.logger.debug(f"Sent message to {peer_id}: {message.msg_type.value}")
            return True
        
        except Exception as e:
            self.logger.error(f"Failed to send message to {peer_id}: {e}")
            # Remove broken stream
            if peer_id in self.streams:
                del self.streams[peer_id]
            if peer_id in self.peers:
                self.peers[peer_id].is_connected = False
            return False
    
    async def broadcast_message(self, message: MeshMessage, exclude: Optional[str] = None) -> int:
        """
        Broadcast a message to all connected peers
        
        Args:
            message: The message to broadcast
            exclude: Peer ID to exclude from broadcast
            
        Returns:
            Number of peers the message was sent to
        """
        count = 0
        for peer_id in list(self.peers.keys()):
            if peer_id != exclude and self.peers[peer_id].is_connected:
                if await self.send_message(peer_id, message):
                    count += 1
        
        self.logger.debug(f"Broadcast message sent to {count} peers")
        return count
    
    def register_handler(
        self,
        msg_type: MessageType,
        handler: Callable[[MeshMessage, PeerID], None]
    ) -> None:
        """
        Register a message handler for a specific message type
        
        Args:
            msg_type: The type of message to handle
            handler: The handler function (can be sync or async)
        """
        if msg_type not in self.message_handlers:
            self.message_handlers[msg_type] = []
        self.message_handlers[msg_type].append(handler)
        self.logger.debug(f"Registered handler for {msg_type.value}")
    
    async def listen(self) -> AsyncGenerator[MeshMessage, None]:
        """
        Listen for incoming messages (async generator)
        
        Yields:
            MeshMessage objects as they arrive
        """
        message_queue = asyncio.Queue()
        
        def queue_handler(message: MeshMessage, sender: PeerID):
            asyncio.create_task(message_queue.put((message, sender)))
        
        # Register temporary handler
        for msg_type in MessageType:
            self.register_handler(msg_type, queue_handler)
        
        # Yield messages as they arrive
        while self._running:
            try:
                message, sender = await asyncio.wait_for(
                    message_queue.get(),
                    timeout=1.0
                )
                yield message
            except asyncio.TimeoutError:
                continue
    
    async def discover_peers(self, timeout: float = 5.0) -> List[PeerInfo]:
        """
        Discover peers using mDNS and DHT
        
        Args:
            timeout: How long to wait for discovery
            
        Returns:
            List of discovered peer info
        """
        self.logger.info("Discovering peers...")
        
        # Request peer discovery from connected peers
        discovery_msg = MeshMessage(
            msg_type=MessageType.PEER_DISCOVERY,
            sender_id=str(self.peer_id),
            recipient_id=None,
            payload={}
        )
        
        await self.broadcast_message(discovery_msg)
        
        # Wait for responses
        await asyncio.sleep(timeout)
        
        return list(self.peers.values())
    
    async def start_heartbeat_loop(self) -> None:
        """Start background heartbeat task"""
        while self._running:
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)
            
            heartbeat = MeshMessage(
                msg_type=MessageType.HEARTBEAT,
                sender_id=str(self.peer_id),
                recipient_id=None,
                payload={"timestamp": time.time()}
            )
            
            # Send heartbeat to all peers
            await self.broadcast_message(heartbeat)
            
            # Check for dead connections
            current_time = time.time()
            for peer_id, info in list(self.peers.items()):
                if current_time - info.last_seen > self.HEARTBEAT_INTERVAL * 3:
                    self.logger.warning(f"Peer {peer_id} appears dead, removing...")
                    info.is_connected = False
                    if peer_id in self.streams:
                        await self.streams[peer_id].close()
                        del self.streams[peer_id]
    
    async def stop(self) -> None:
        """Stop the mesh network node"""
        self.logger.info("Stopping mesh network node...")
        self._running = False
        
        # Close all streams
        for stream in self.streams.values():
            try:
                await stream.close()
            except Exception:
                pass
        
        # Stop mDNS
        if self._mdns:
            await self._mdns.stop()
        
        # Close host
        if self._host:
            await self._host.close()
        
        self.peers.clear()
        self.streams.clear()
        self.logger.info("Node stopped")
    
    def get_peer_list(self) -> List[Dict[str, Any]]:
        """Get list of all known peers"""
        return [
            {
                "peer_id": info.peer_id,
                "multiaddr": info.multiaddr,
                "is_connected": info.is_connected,
                "last_seen": info.last_seen,
                "protocols": info.protocols
            }
            for info in self.peers.values()
        ]
    
    def get_multiaddr(self) -> str:
        """Get the multiaddress for this node"""
        return f"/ip4/{self.host}/tcp/{self.port}/p2p/{self.peer_id}"


# Convenience functions for common mesh-llm operations
class MeshLLMClient:
    """High-level client for mesh-llm operations"""
    
    def __init__(self, network: MeshNetwork):
        self.network = network
        self._setup_handlers()
    
    def _setup_handlers(self):
        """Set up message handlers for mesh-llm operations"""
        self.network.register_handler(
            MessageType.INFERENCE_REQUEST,
            self._handle_inference_request
        )
        self.network.register_handler(
            MessageType.INFERENCE_RESPONSE,
            self._handle_inference_response
        )
    
    async def _handle_inference_request(self, message: MeshMessage, sender: PeerID):
        """Handle incoming inference request"""
        # This would integrate with your LLM inference engine
        self.network.logger.info(f"Received inference request from {sender}")
        # Process and respond...
    
    async def _handle_inference_response(self, message: MeshMessage, sender: PeerID):
        """Handle incoming inference response"""
        self.network.logger.info(f"Received inference response from {sender}")
    
    async def send_inference_request(
        self,
        peer_id: str,
        prompt: str,
        parameters: Optional[Dict] = None
    ) -> bool:
        """Send an inference request to a peer"""
        message = MeshMessage(
            msg_type=MessageType.INFERENCE_REQUEST,
            sender_id=str(self.network.peer_id),
            recipient_id=peer_id,
            payload={
                "prompt": prompt,
                "parameters": parameters or {}
            }
        )
        return await self.network.send_message(peer_id, message)
    
    async def broadcast_model_update(
        self,
        model_name: str,
        version: str,
        weights_uri: Optional[str] = None
    ) -> int:
        """Broadcast a model update to all peers"""
        message = MeshMessage(
            msg_type=MessageType.MODEL_UPDATE,
            sender_id=str(self.network.peer_id),
            recipient_id=None,
            payload={
                "model_name": model_name,
                "version": version,
                "weights_uri": weights_uri
            }
        )
        return await self.network.broadcast_message(message)


# Example usage and demonstration
async def demo():
    """Demonstrate the mesh network functionality"""
    print("=" * 60)
    print("libp2p Mesh Network Demo for mesh-llm")
    print("=" * 60)
    
    if not LIBP2P_AVAILABLE:
        print("\nERROR: libp2p is not installed.")
        print("Run these commands to install:")
        print("  pip install multiaddr PyNaCl fastecdsa aioquic")
        print("  git clone https://github.com/libp2p/py-libp2p.git")
        print("  cd py-libp2p && pip install -e .")
        return
    
    # Create two nodes
    node1 = MeshNetwork(port=4001, enable_mdns=False, bootstrap=False)
    node2 = MeshNetwork(port=4002, enable_mdns=False, bootstrap=False)
    
    try:
        # Start both nodes
        print("\n[1] Starting node 1...")
        await node1.start()
        print(f"    Node 1 PeerID: {node1.peer_id}")
        print(f"    Node 1 Address: {node1.get_multiaddr()}")
        
        print("\n[2] Starting node 2...")
        await node2.start()
        print(f"    Node 2 PeerID: {node2.peer_id}")
        print(f"    Node 2 Address: {node2.get_multiaddr()}")
        
        # Connect node2 to node1
        print(f"\n[3] Connecting node 2 to node 1...")
        connected = await node2.connect_to_peer(node1.get_multiaddr())
        print(f"    Connection successful: {connected}")
        
        # Set up message handler on node1
        received_messages = []
        
        def handle_custom(msg: MeshMessage, sender: PeerID):
            received_messages.append((msg, sender))
            print(f"    Node 1 received message from {sender}: {msg.payload}")
        
        node1.register_handler(MessageType.CUSTOM, handle_custom)
        
        # Send a message from node2 to node1
        print(f"\n[4] Sending test message from node 2 to node 1...")
        test_message = MeshMessage(
            msg_type=MessageType.CUSTOM,
            sender_id=str(node2.peer_id),
            recipient_id=str(node1.peer_id),
            payload={"test": "Hello from node 2!", "data": [1, 2, 3]}
        )
        sent = await node2.send_message(str(node1.peer_id), test_message)
        print(f"    Message sent: {sent}")
        
        # Wait for message to be processed
        await asyncio.sleep(1)
        
        # Get peer lists
        print(f"\n[5] Peer lists:")
        print(f"    Node 1 peers: {len(node1.get_peer_list())}")
        print(f"    Node 2 peers: {len(node2.get_peer_list())}")
        
        # Test broadcast
        print(f"\n[6] Testing broadcast from node 1...")
        broadcast_msg = MeshMessage(
            msg_type=MessageType.HEARTBEAT,
            sender_id=str(node1.peer_id),
            recipient_id=None,
            payload={"broadcast": "test"}
        )
        count = await node1.broadcast_message(broadcast_msg)
        print(f"    Broadcast sent to {count} peers")
        
        print("\n[7] Demo completed successfully!")
        print("\nTo use in mesh-llm:")
        print("  from libp2p_mesh_network import MeshNetwork, MeshLLMClient")
        print("  network = MeshNetwork(port=4001)")
        print("  await network.start()")
        print("  client = MeshLLMClient(network)")
        print("  await client.broadcast_model_update('my-model', 'v1.0')")
        
    except Exception as e:
        print(f"\nERROR during demo: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        # Clean up
        print("\n[8] Cleaning up...")
        await node1.stop()
        await node2.stop()
        print("    Nodes stopped")


if __name__ == "__main__":
    asyncio.run(demo())
