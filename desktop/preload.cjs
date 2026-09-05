const { contextBridge, ipcRenderer } = require('electron');
// Each bridge method represents one supported operation. Never expose send,
// invoke, fetch, arbitrary filesystem reads, or the owner credential itself.
const methods = ['status', 'connect', 'disconnect', 'recovery', 'projects', 'tasks', 'revisions',
  'threads', 'messages', 'decisions', 'studies', 'study', 'studySelection', 'saveStudyExport', 'createStudy', 'registerVariant', 'recordVariant', 'closeStudy', 'createSource', 'assessSource', 'correctSource', 'createFinding', 'acceptFinding', 'labbook', 'createProject', 'createTask',
  'createThread', 'sendMessage', 'resolveDecision', 'approve', 'commandId'];
contextBridge.exposeInMainWorld('tessera', Object.fromEntries(methods.map(method => [method,
  (...args) => ipcRenderer.invoke(`tessera:${method}`, ...args)])));
