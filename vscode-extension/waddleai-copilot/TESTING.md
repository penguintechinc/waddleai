# WaddleAI VS Code Extension - Testing Guide

## Extension Status: ✅ READY FOR TESTING

The WaddleAI VS Code extension has been successfully compiled and is ready for testing. All TypeScript files have been compiled to JavaScript without errors.

## Manual Testing Instructions

### Prerequisites
1. Ensure VS Code is installed on your system
2. Have a WaddleAI API key ready (format: `wa-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`)
3. WaddleAI proxy server should be running (default: `http://localhost:8000`)

### Step 1: Launch Extension Development Host
1. Open this folder (`vscode-extension/waddleai-copilot`) in VS Code
2. Press `F5` or go to Run menu → Start Debugging
3. This will launch a new VS Code window titled "[Extension Development Host]"

### Step 2: Configure WaddleAI Extension
1. In the Extension Development Host window, open Command Palette (`Ctrl+Shift+P`)
2. Type "WaddleAI: Set API Key" and select it
3. Enter your WaddleAI API key when prompted
4. The extension will automatically test the connection

### Step 3: Test Chat Participant
1. Open the Chat panel in VS Code (View → Chat or `Ctrl+Alt+I`)
2. In the chat input, type `@waddleai` followed by your message
3. Example: `@waddleai Hello, can you help me write a Python function?`
4. The extension should stream responses from WaddleAI

### Step 4: Test Configuration Commands
Test these commands via Command Palette:
- **WaddleAI: Select Model** - Choose from available models
- **WaddleAI: Test Connection** - Verify API connectivity  
- **WaddleAI: Show Token Usage** - View usage statistics
- **WaddleAI: Clear Conversation Memory** - Reset conversation history

## Expected Behavior

### ✅ Success Indicators
- Chat participant `@waddleai` appears in autocomplete
- Responses stream in real-time with markdown formatting
- Error messages display helpful troubleshooting buttons
- Configuration commands work without errors
- Usage statistics display in webview panel

### ❌ Potential Issues
- **"WaddleAI request failed"** → Check API key and server connection
- **"Authentication failed"** → Verify API key format and validity
- **Chat participant not found** → Extension may not have activated properly

## Technical Test Results

### ✅ Compilation Status
```
✅ All TypeScript files compiled successfully
✅ No compilation errors or warnings
✅ Source maps generated for debugging
✅ Extension manifest (package.json) validates
```

### ✅ File Structure
```
out/
├── extension.js (Main extension entry point)
├── chatParticipant.js (Chat participant implementation)
├── waddleaiClient.js (WaddleAI API client)
├── authProvider.js (Authentication provider)
└── *.js.map (Source maps for debugging)
```

### ✅ Extension Configuration
- **Activation**: `onStartupFinished`
- **Commands**: 5 registered commands
- **Chat Participant**: `waddleai`
- **Configuration**: 7 settings properties

## Architecture Overview

```
VS Code Chat Panel
       ↓
WaddleAIChatParticipant
       ↓
WaddleAIClient (HTTP/Axios)
       ↓
WaddleAI Proxy Server
       ↓
Various LLM Providers (GPT-4, Claude, etc.)
```

## Debug Information

### Extension Logs
Check the Debug Console in VS Code for extension logs:
1. View → Debug Console
2. Look for "[WaddleAI]" prefixed messages

### Network Debugging
Monitor network requests in the extension:
1. Open Developer Tools: Help → Toggle Developer Tools
2. Go to Network tab
3. Look for requests to your WaddleAI endpoint

## Known Limitations
- Node.js 18.x compatibility (vsce packaging requires Node 20+)
- Extension cannot be packaged to VSIX without Node.js upgrade
- Manual testing required via Extension Development Host

## Next Steps for Production
1. Upgrade Node.js to version 20+ for VSIX packaging
2. Add comprehensive unit tests
3. Test with various WaddleAI models
4. Add error logging and telemetry
5. Publish to VS Code Marketplace