/** Authentication of renderer IPC senders, independent of message content. */
export function validateSender(event, window) {
  if (!window || event.sender !== window.webContents ||
      event.senderFrame !== window.webContents.mainFrame ||
      event.senderFrame.url !== 'tessera://app/index.html') {
    throw new Error('Untrusted desktop message.');
  }
}
