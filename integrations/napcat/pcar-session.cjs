// Project-owned loader adapter. Vendor files and the plugin whitelist stay intact.
// This file is selected through NapCat's documented NAPCAT_LOAD_PATH entrypoint.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const root = process.env.PCAR_SESSION_ROOT;
const token = process.env.PCAR_SESSION_TOKEN;
const instance = process.env.PCAR_SESSION_INSTANCE;
const bot = process.env.PCAR_SESSION_BOT;
let wrapper, login, session, sessionConfig, busy = false, watcher;
function write(name, data) {
  const target = path.join(root, name);
  const tmp = target + '.' + crypto.randomUUID() + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(data), {mode: 0o600});
  fs.renameSync(tmp, target);
}
function capability() {
  return {instance, bot, pid: process.pid, offline: typeof login?.offline === 'function',
    sessionOffline: typeof session?.offLine === 'function', capturedSessionConfig: !!sessionConfig,
    sessionMethods: session ? Object.getOwnPropertyNames(Object.getPrototypeOf(session)).filter(k => /line|init|self|account/i.test(k)) : [],
    msfStatus: typeof login?.getMsfStatus === 'function' ? login.getMsfStatus() : null};
}
async function command() {
  if (busy || !root || !token) return;
  busy = true;
  try {
    const request = JSON.parse(fs.readFileSync(path.join(root, 'request.json'), 'utf8'));
    if (request.token !== token || request.instance !== instance || request.bot !== bot ||
        !/^[a-f0-9-]{36}$/.test(request.id) || Math.abs(Date.now()/1000-request.at) > 15) return;
    const response = request.id + '.json';
    if (fs.existsSync(path.join(root, response))) return;
    let result = capability();
    if (request.action === 'logout') {
      if (fs.existsSync(path.join(root, 'logout-invocation.json')))
        result = {...result, invoked: false, reason: 'logout_effect_requires_reconciliation'};
      else if (sessionConfig?.selfUin !== bot || (!result.offline && !result.sessionOffline))
        result = {...result, invoked: false, reason: 'scoped_logout_unsupported'};
      else {
        write('logout-invocation.json', {id: request.id, instance, at: Date.now()/1000});
        const method = result.offline ? 'login.offline' : 'session.offLine';
        // Reuse the EXACT config passed to init by NapCat after authentication.
        // It stays in memory, including native ticket fields; never synthesize it.
        try {
          if (result.offline) await login.offline();
          else await session.offLine(sessionConfig);
          result = {...capability(), invoked: true, method};
        } catch (error) {
          result = {...capability(), invoked: true, method, reason: 'native_logout_unconfirmed'};
        }
      }
    } else if (request.action !== 'capability') return;
    write(response, {...result, id: request.id, at: Date.now()/1000});
  } catch (error) {
    // No raw wrapper errors or account material in ordinary logs.
  } finally { busy = false; }
}
if (root && token && instance && bot) {
  fs.mkdirSync(root, {recursive:true});
  write('adapter-started.json', {instance, pid: process.pid, at: Date.now()/1000});
  const original = process.dlopen;
  process.dlopen = function(module, filename, ...args) {
    const result = original.call(this, module, filename, ...args);
    if (module.exports?.NodeIKernelLoginService) {
      wrapper = module.exports;
      login = wrapper.NodeIKernelLoginService.get();
      const sessions = wrapper.NodeIQQNTWrapperSession;
      const facade = new Proxy({}, {get(_, key) {
        const value = sessions[key];
        if (typeof value !== 'function') return value;
        return (...args) => {
          const result = value.apply(sessions, args);
          if ((key === 'getNTWrapperSession' || key === 'create') && result) {
            if (session !== result) sessionConfig = undefined;
            session = result;
            return new Proxy({}, {get(_, method) {
              const fn = result[method];
              if (typeof fn !== 'function') return fn;
              return (...params) => {
                if (method === 'init') sessionConfig = params[0]?.selfUin === bot ? params[0] : undefined;
                return fn.apply(result, params);
              };
            }});
          }
          return result;
        };
      }});
      module.exports = {...wrapper, NodeIQQNTWrapperSession: facade};
      process.dlopen = original;
      write('capability.json', {...capability(), at: Date.now()/1000});
      watcher = fs.watch(root, (_, name) => { if (name === 'request.json') void command(); });
      watcher.unref();
    }
    return result;
  };
}
// Loading is otherwise identical to the upstream entrypoint.
void import(require('node:url').pathToFileURL(process.env.NAPCAT_MAIN_PATH).href);
