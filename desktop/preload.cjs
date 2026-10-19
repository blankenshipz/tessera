const { contextBridge, ipcRenderer } = require('electron');
// Sandboxed Electron preloads deliberately cannot load local CommonJS modules.
// Keep this bridge self-contained; ipc-contract.test.mjs checks this exact list
// against the main-process contract.
const methods = Object.freeze([
  'autonomySessions',
  'artifactDispositions', 'recordArtifactDisposition',
  'remoteExecutions', 'remoteExecution', 'remoteExecutionCommand',
  'adminWorkspace', 'adminCreate', 'adminCommand', 'adminAudit',
  'issueOperatorControlCredential',
  'secretContext', 'protectedSecret',
  'status', 'connect', 'disconnect', 'recovery', 'projects', 'tasks', 'workers', 'revisions', 'taskPlan', 'reviseTaskPlan',
  'threads', 'messages', 'decisions', 'programContext', 'experiments', 'experiment', 'createExperiment', 'reviseExperiment', 'admitExperimentAttempt', 'dispatchAdmittedAttempt', 'resolveAdmittedAttempt', 'linkAttemptArtifacts', 'recordMeasurement', 'createConclusion', 'updateConclusion', 'reviewConclusion', 'amendConclusion', 'remoteWorkflows', 'remoteWorkflow', 'executionGrant', 'revokeExecutionGrant', 'recoveryReconciliations', 'reconcileLostWorkerOperation', 'studies', 'study', 'artifacts', 'artifactManifests', 'recordArtifactCustodyRelease', 'releasedArtifactManifest', 'artifactPreview', 'saveReleasedArtifact', 'exportReproductionBundle', 'reproductionBundle', 'studySelection', 'saveStudyExport', 'createStudy', 'registerVariant', 'recordVariant', 'closeStudy', 'createSource', 'assessSource', 'correctSource', 'createFinding', 'acceptFinding', 'reaffirmFinding', 'retractFinding', 'reviseFinding', 'labbook', 'createProject', 'createTask',
  'createThread', 'sendMessage', 'harnessSessionBinding', 'enqueueHarnessDelivery', 'cancelHarnessDelivery', 'astraConversation', 'astraProposals', 'coordinatorConsumerStatus', 'requestCoordinatorTurn', 'explainStatusTurns', 'requestExplainStatusTurn', 'registerProposalDesign', 'resolveDecision', 'createDecision', 'approve', 'commandId',
  'namespaceSelectKubeconfig', 'namespaceInspect', 'namespacePlanUseExisting', 'namespacePlanCreate', 'namespaceCreate', 'namespaceStatus', 'namespaceReconcile', 'namespaceClear',
]);
// Each bridge method represents one supported operation. Never expose send,
// invoke, fetch, arbitrary filesystem reads, or the owner credential itself.
contextBridge.exposeInMainWorld('tessera', Object.fromEntries(methods.map(method => [method,
  (...args) => ipcRenderer.invoke(`tessera:${method}`, ...args)])));
