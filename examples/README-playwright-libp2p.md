# Playwright Automation and libp2p Mesh Network Setup

This directory contains automation scripts and a complete libp2p implementation for mesh-llm networking.

## Files

1. **playwright_colab_automation.js** - Playwright script for automating Jupyter Lab setup in Google Colab
2. **python3/libp2p_mesh_network.py** - Complete libp2p mesh network implementation for mesh-llm

---

## 1. Playwright Colab Automation

### Overview
This script automates the process of:
1. Opening the Colab notebook at `https://colab.research.google.com/github/scatterp2/webvm/blob/main/jupyter%20(1).ipynb`
2. Pressing **Ctrl+F9** to execute the first cell (starts Jupyter Lab)
3. Waiting for and extracting the Jupyter Lab URL from the output
4. Opening Jupyter Lab in a new tab
5. Creating a new notebook in Jupyter Lab
6. Pressing **Shift+Enter twice** to execute cells

### Prerequisites

Install Playwright:
```bash
npm install playwright
npx playwright install chromium
```

Or if using the debian_mini Dockerfile, it already includes:
```dockerfile
RUN npm install -g playwright
```

### Usage

Run the automation script:
```bash
node examples/playwright_colab_automation.js
```

### Key Features

- **Waits for new page/tab**: The script properly waits for the Jupyter Lab URL to appear in the Colab output before opening it in a new tab
- **Extracts dynamic URLs**: Uses regex to find the proxy URL (which changes each session)
- **Proper timing**: Includes appropriate waits between actions to ensure UI stability
- **Error handling**: Takes screenshots on failure for debugging
- **Headless option**: Can run in headless mode by setting `headless: true`

### Script Flow

```javascript
1. Launch browser → 2. Open Colab notebook → 3. Wait for load
   ↓
4. Press Ctrl+F9 → 5. Wait for output → 6. Extract Jupyter URL
   ↓
7. Open new tab → 8. Navigate to Jupyter Lab → 9. Wait for interface
   ↓
10. Create new notebook → 11. Press Shift+Enter twice → 12. Done
```

### Customization

Edit these values in the script as needed:
- `headless`: Set to `true` for headless execution
- `timeout`: Adjust timeout values for slower connections
- Port number: Change `PORT_NUM` in the Colab notebook if needed

---

## 2. libp2p Mesh Network for mesh-llm

### Overview

A complete, production-ready libp2p implementation that enables:
- **Peer discovery** via mDNS and bootstrap nodes
- **Secure encrypted communication** between instances
- **Message routing** and forwarding
- **Connection management** with health monitoring
- **Multiple transport protocols** (TCP, WebSocket, QUIC)
- **NAT traversal** via circuit relay

### Installation

In your Jupyter notebook or Python environment:

```python
# Install core dependencies
!pip install multiaddr PyNaCl fastecdsa aioquic

# Clone and install py-libp2p
!git clone https://github.com/libp2p/py-libp2p.git
%cd py-libp2p
!pip install -e .
%cd ..
```

### Quick Start

```python
import asyncio
from libp2p_mesh_network import MeshNetwork, MeshLLMClient, MessageType

async def main():
    # Create and start a node
    network = MeshNetwork(port=4001)
    await network.start()
    
    print(f"My PeerID: {network.peer_id}")
    print(f"My address: {network.get_multiaddr()}")
    
    # Connect to another peer
    await network.connect_to_peer("/ip4/192.168.1.100/tcp/4001/p2p/QmPeerID...")
    
    # Send a message
    from libp2p_mesh_network import MeshMessage
    msg = MeshMessage(
        msg_type=MessageType.CUSTOM,
        sender_id=str(network.peer_id),
        recipient_id="QmTargetPeerID...",
        payload={"hello": "world"}
    )
    await network.send_message("QmTargetPeerID...", msg)
    
    # Listen for messages
    async for message in network.listen():
        print(f"Received: {message.payload}")
    
    # Stop when done
    await network.stop()

asyncio.run(main())
```

### Using with mesh-llm

```python
from libp2p_mesh_network import MeshNetwork, MeshLLMClient

# Initialize network
network = MeshNetwork(port=4001, enable_mdns=True, bootstrap=True)
await network.start()

# Create high-level client
client = MeshLLMClient(network)

# Broadcast model update to all peers
await client.broadcast_model_update(
    model_name="llama-2-7b",
    version="v1.0",
    weights_uri="huggingface://meta-llama/Llama-2-7b"
)

# Send inference request to specific peer
await client.send_inference_request(
    peer_id="QmPeerID...",
    prompt="What is the meaning of life?",
    parameters={"max_tokens": 100, "temperature": 0.7}
)

# Discover peers
peers = await network.discover_peers(timeout=5.0)
print(f"Discovered {len(peers)} peers:")
for peer in peers:
    print(f"  - {peer.peer_id}: {peer.multiaddr}")
```

### Message Types

The network supports these message types:

| Type | Description |
|------|-------------|
| `PEER_DISCOVERY` | Request list of known peers |
| `PEER_RESPONSE` | Response with peer information |
| `INFERENCE_REQUEST` | Request LLM inference |
| `INFERENCE_RESPONSE` | Return inference results |
| `MODEL_UPDATE` | Broadcast model changes |
| `HEARTBEAT` | Keep-alive signal |
| `CUSTOM` | Custom application messages |

### Architecture

```
┌─────────────────┐         ┌─────────────────┐
│   Node 1        │         │   Node 2        │
│  PeerID: QmA... │◄───────►│  PeerID: QmB... │
│  Port: 4001     │  TCP/   │  Port: 4002     │
│                 │  WS/QUIC│                 │
└────────┬────────┘         └────────┬────────┘
         │                           │
         │    ┌──────────────┐       │
         └───►│  Bootstrap   │◄──────┘
              │   Nodes      │
              └──────────────┘
```

### Running the Demo

```bash
cd examples/python3
python3 libp2p_mesh_network.py
```

This will:
1. Start two nodes on ports 4001 and 4002
2. Connect them together
3. Send test messages
4. Demonstrate broadcast functionality
5. Show peer discovery

### Advanced Configuration

```python
# Full configuration options
network = MeshNetwork(
    port=4001,              # Listening port
    host="0.0.0.0",         # Bind address
    enable_mdns=True,       # Local peer discovery
    enable_relay=True,      # NAT traversal
    bootstrap=True          # Connect to bootstrap nodes
)

# Register custom message handler
def handle_my_message(message, sender):
    print(f"Custom message from {sender}: {message.payload}")

network.register_handler(MessageType.CUSTOM, handle_my_message)

# Start heartbeat monitoring
await network.start_heartbeat_loop()
```

### Error Handling

The network includes comprehensive error handling:
- Automatic reconnection attempts
- Dead peer detection and removal
- Message delivery confirmation
- Stream error recovery
- Graceful shutdown

### Integration with Existing Code

To integrate with your existing mesh-llm setup:

1. Replace basic networking code with `MeshNetwork`
2. Use `MeshMessage` for all inter-node communication
3. Register handlers for message types you care about
4. Use `MeshLLMClient` for high-level operations

Example migration:
```python
# Before: Basic socket communication
socket.send(json.dumps(data))

# After: libp2p mesh network
msg = MeshMessage(
    msg_type=MessageType.INFERENCE_REQUEST,
    sender_id=str(network.peer_id),
    recipient_id=target_peer,
    payload=data
)
await network.send_message(target_peer, msg)
```

---

## Troubleshooting

### Playwright Issues

**Problem**: Script fails to find Colab elements
- **Solution**: Increase wait times, check internet connection, verify Colab URL

**Problem**: Ctrl+F9 doesn't execute cell
- **Solution**: Ensure focus is on the code cell, try clicking the cell first

**Problem**: Can't extract Jupyter URL
- **Solution**: Check Colab output format, adjust regex pattern

### libp2p Issues

**Problem**: Import errors
- **Solution**: Ensure all dependencies are installed in correct order

**Problem**: Can't connect to peers
- **Solution**: Check firewall settings, verify multiaddress format, ensure both nodes are running

**Problem**: Messages not received
- **Solution**: Verify message handlers are registered, check stream state

---

## License

Same as parent project (see LICENSE.txt in root directory)
