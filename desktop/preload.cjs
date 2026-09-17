const { contextBridge, ipcRenderer } = require('electron');
// Each bridge method represents one supported operation. Never expose send,
// invoke, fetch, arbitrary filesystem reads, or the owner credential itself.
const methods = ['status', 'connect', 'disconnect', 'recovery', 'projects', 'tasks', 'workers', 'revisions', 'taskPlan',
  'threads', 'messages', 'decisions', 'experiments', 'experiment', 'remoteWorkflows', 'remoteWorkflow', 'executionGrant', 'revokeExecutionGrant', 'studies', 'study', 'studySelection', 'saveStudyExport', 'createStudy', 'registerVariant', 'recordVariant', 'closeStudy', 'createSource', 'assessSource', 'correctSource', 'createFinding', 'acceptFinding', 'reaffirmFinding', 'retractFinding', 'reviseFinding', 'labbook', 'createProject', 'createTask',
  'createThread', 'sendMessage', 'harnessSessionBinding', 'enqueueHarnessDelivery', 'cancelHarnessDelivery', 'astraConversation', 'astraProposals', 'requestCoordinatorTurn', 'explainStatusTurns', 'requestExplainStatusTurn', 'registerProposalDesign', 'resolveDecision', 'approve', 'commandId'];
contextBridge.exposeInMainWorld('tessera', Object.fromEntries(methods.map(method => [method,
  (...args) => ipcRenderer.invoke(`tessera:${method}`, ...args)])));
