/** Main-process-only, bounded native helper. No secret value is returned to main/renderer. */
import { spawn } from 'node:child_process';
import { isAbsolute } from 'node:path';

const failure = () => new Error('Protected secret operation unavailable or uncertain. Inspect metadata; never automatically retry. Local macOS Keychain and an explicitly configured same-host helper are required.');

export function validateProtectedRequest(request) {
  const keys = ['project_id', 'record_id', 'expected_revision', 'enrollment_id', 'operation'];
  if (!request || typeof request !== 'object' || Array.isArray(request) ||
      Object.keys(request).length !== keys.length || Object.keys(request).some(key => !keys.includes(key)) ||
      ['project_id', 'record_id', 'enrollment_id', 'operation'].some(key => typeof request[key] !== 'string') ||
      !/^[a-zA-Z0-9_-]{1,128}$/.test(request.project_id) || !/^admin_[a-f0-9]{32}$/.test(request.record_id) ||
      !/^[a-f0-9]{32}$/.test(request.enrollment_id) || !Number.isSafeInteger(request.expected_revision) || request.expected_revision < 1 ||
      !['enroll', 'import', 'health', 'purge'].includes(request.operation)) throw failure();
}

export function runProtectedHelper({ token, request, signal, current, python, stateDir,
  platform = process.platform, spawnImpl = spawn }) {
  validateProtectedRequest(request);
  if (platform !== 'darwin' || !python || !stateDir || !isAbsolute(python) || !isAbsolute(stateDir) || signal.aborted) throw failure();
  current();
  return new Promise((resolve, reject) => {
    const child = spawnImpl(python, ['-m', 'tessera_control.secret_enrollment_cli', '--state-dir', stateDir], {
      stdio: ['pipe', 'pipe', 'ignore'], shell: false,
      env: { PATH: '/usr/bin:/bin', LANG: 'en_US.UTF-8' },
    });
    let settled = false, pending = '', size = 0, confirmed = false, result = null;
    const finish = (error) => {
      if (settled) return;
      settled = true; clearTimeout(timer); signal.removeEventListener('abort', abort);
      child.stdin.destroy(); child.stdout.destroy(); pending = '';
      if (child.exitCode == null && child.signalCode == null) {
        child.kill('SIGTERM');
        const force = setTimeout(() => child.kill('SIGKILL'), 2000);
        force.unref(); child.once('close', () => clearTimeout(force));
      }
      if (error) reject(failure()); else resolve(result);
    };
    const abort = () => finish(true);
    const timer = setTimeout(abort, 90000);
    signal.addEventListener('abort', abort, { once: true });
    child.on('error', abort); child.stdin.on('error', abort); child.stdout.on('error', abort);
    child.stdout.on('data', data => {
      size += data.length;
      if (size > 4096) return abort();
      pending += data.toString('utf8');
      try {
        while (pending.includes('\n')) {
          const end = pending.indexOf('\n'), line = pending.slice(0, end); pending = pending.slice(end + 1);
          const message = JSON.parse(line);
          current();
          if (message.phase === 'confirm' && Object.keys(message).length === 1 && !confirmed && !result && ['enroll', 'import'].includes(request.operation)) {
            confirmed = true;
            child.stdin.write('{"commit":true}\n');
          } else if (message.phase === 'result' && !result && (confirmed || ['health', 'purge'].includes(request.operation))) {
            if (message.store !== 'macos_keychain' || !Number.isSafeInteger(message.version) || message.version < 1 ||
                Object.keys(message).some(key => !['phase', 'version', 'state', 'cleanup_pending', 'store', 'available'].includes(key)) ||
                (message.state !== undefined && !['active', 'revoked', 'expired', 'deleted'].includes(message.state)) ||
                (message.available !== undefined && typeof message.available !== 'boolean') ||
                (message.cleanup_pending !== undefined && typeof message.cleanup_pending !== 'boolean')) throw failure();
            // Closed metadata projection, never pass arbitrary subprocess JSON through.
            result = { version: message.version, state: message.state, cleanupPending: message.cleanup_pending,
              store: 'macos_keychain', available: message.available, providerEnabled: false };
          } else throw failure();
        }
      } catch { abort(); }
    });
    child.on('close', code => {
      try { current(); finish(code !== 0 || !result || pending.length > 0); } catch { abort(); }
    });
    const input = Buffer.from(JSON.stringify({ token, request }) + '\n');
    child.stdin.write(input, () => input.fill(0));
    if (signal.aborted) abort();
  });
}
