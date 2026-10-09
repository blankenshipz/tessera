'use strict';

// This is the complete renderer-to-main capability list.  Keep it deliberately
// operation-shaped: adding a name here is a security review, not a convenience.
const rendererMethods = Object.freeze([
  'autonomySessions',
  'artifactDispositions', 'recordArtifactDisposition',
  'adminWorkspace', 'adminCreate', 'adminCommand', 'adminAudit',
  'secretContext', 'protectedSecret',
  'remoteExecutions', 'remoteExecution', 'remoteExecutionCommand',
  'status', 'connect', 'disconnect', 'recovery', 'projects', 'tasks', 'workers', 'revisions', 'taskPlan', 'reviseTaskPlan',
  'threads', 'messages', 'decisions', 'programContext', 'experiments', 'experiment', 'createExperiment', 'reviseExperiment', 'admitExperimentAttempt', 'dispatchAdmittedAttempt', 'resolveAdmittedAttempt', 'linkAttemptArtifacts', 'recordMeasurement', 'createConclusion', 'updateConclusion', 'reviewConclusion', 'amendConclusion', 'remoteWorkflows', 'remoteWorkflow', 'executionGrant', 'revokeExecutionGrant', 'recoveryReconciliations', 'reconcileLostWorkerOperation', 'studies', 'study', 'artifacts', 'artifactManifests', 'recordArtifactCustodyRelease', 'releasedArtifactManifest', 'artifactPreview', 'saveReleasedArtifact', 'exportReproductionBundle', 'reproductionBundle', 'studySelection', 'saveStudyExport', 'createStudy', 'registerVariant', 'recordVariant', 'closeStudy', 'createSource', 'assessSource', 'correctSource', 'createFinding', 'acceptFinding', 'reaffirmFinding', 'retractFinding', 'reviseFinding', 'labbook', 'createProject', 'createTask',
  'createThread', 'sendMessage', 'harnessSessionBinding', 'enqueueHarnessDelivery', 'cancelHarnessDelivery', 'astraConversation', 'astraProposals', 'coordinatorConsumerStatus', 'requestCoordinatorTurn', 'explainStatusTurns', 'requestExplainStatusTurn', 'registerProposalDesign', 'resolveDecision', 'createDecision', 'approve', 'commandId',
  // Namespace lifecycle: opaque native kubeconfig references only.  These are
  // not a shell, kubectl, filesystem, apply, patch, delete, or Secret bridge.
  'namespaceSelectKubeconfig', 'namespaceInspect', 'namespacePlanUseExisting', 'namespacePlanCreate', 'namespaceCreate', 'namespaceStatus', 'namespaceReconcile', 'namespaceClear',
]);

module.exports = { rendererMethods };
