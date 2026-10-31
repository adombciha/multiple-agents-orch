"""Offline simulated DOM tests executing the rendered GUI script with Node.js.

These are not real browser tests: layout, native focus behavior, and HTML parsing
are not verified. No npm packages, network access, or browser dependencies are used.
"""

import re
import shutil
import subprocess

import pytest

from orchestrator.gui import render_page

HARNESS = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = require('node:fs').readFileSync(0, 'utf8');
const fragments = new Map();
const mutations = [];
class Element {
  constructor(html='', children=[], dataset={}) {
    this._html = html; this.children = children; this.dataset = dataset;
    this.scrollTop = 0; this.scrollLeft = 0; this.hidden = false;
    this.disabled = false; this.textContent = ''; this.open = false;
    children.forEach(child => child.parent = this);
    fragments.set(html, children);
  }
  get innerHTML() { return this._html; }
  set innerHTML(value) {
    mutations.push(value);
    this.children.forEach(child => child.parent = null);
    this.children = fragments.get(value) || [];
    this.children.forEach(child => child.parent = this);
    this._html = value;
  }
  contains(node) { return this === node || this.children.some(child => child.contains(node)); }
  querySelector() { return this.children[0]; }
  appendChild(child) { this.children.push(child); child.parent = this; }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  hasAttribute(name) { return name === 'data-csrf' && 'csrf' in this.dataset; }
  focus() { document.activeElement = this; }
}
function page(version) {
  const pre = new Element('report-' + version);
  const panel = new Element('panel-' + version, [pre], {report:'qa'});
  const input = new Element('input-' + version); input.value = '';
  const submit = new Element('submit-' + version);
  const action = new Element('button-' + version, [], {action:'continue{}'});
  const nodes = {
    state:new Element('state-' + version),
    'last-refresh':new Element(),
    'workflow-status':new Element('phase-' + version), tasks:new Element('tasks-' + version),
    'task-form':new Element('form-' + version, [input, submit]),
    actions:new Element('actions-' + version, [action]),
    git:new Element('impact-' + version, [], {snapshotId:version}),
    reports:new Element('reports-' + version, [panel]), 'refresh-error':new Element('error-' + version),
  };
  const body = new Element('body-' + version, Object.values(nodes), {csrf:'csrf-' + version});
  return {nodes, body, input, submit, action, panel, pre, activeElement:body,
    getElementById(id) { return nodes[id]; },
    querySelector() { return input; },
    querySelectorAll(selector) { return selector === '#actions button' ? nodes.actions.children : [submit, ...nodes.actions.children]; },
    contains(node) { return body.contains(node); },
  };
}
const document = page('old');
const next = page('new');
const stale = page('stale');
const invalid = page('invalid');
let clock = Date.UTC(2026, 0, 2, 3, 4, 5);
const timestampApplications = [];
class LocalDate extends Date {
  constructor(...args) { super(...(args.length ? args : [clock])); }
  toLocaleString(...args) {
    timestampApplications.push(document.nodes['workflow-status'].innerHTML);
    return super.toLocaleString(...args);
  }
}
const timestamp = () => '最後成功更新：' + new Date(clock).toLocaleString();
let selection = {isCollapsed:true};
const alerts = [], timers = [], requests = [];
const window = {scrollX:20, scrollY:400,
  getSelection:() => selection,
  scrollTo(x,y) { this.scrollX=x; this.scrollY=y; },
};
let request = async () => ({ok:true, text:async()=> 'new'});
const context = vm.createContext({document, window, Date:LocalDate,
  DOMParser:class { parseFromString(key) {
    return key === 'invalid' ? invalid : key === 'stale' ? stale : next;
  } },
  fetch:(url, options) => { requests.push({url, options}); return request(url, options); },
  setTimeout:(callback,delay)=>timers.push({callback,delay}),
  alert:message=>alerts.push(message), FormData:class {},
  location:{reload() { throw Error('A full page reload must never occur'); }},
});
vm.runInContext(source, context);
const run = expression => vm.runInContext(expression, context);
const response = {ok:true, json:async()=>({}), text:async()=> 'new'};

(async () => {
  switch (process.argv[1]) {
    case 'initial': {
      assert.equal(document.nodes['last-refresh'].textContent, '');
      assert.equal(document.nodes['refresh-error'].textContent, '');
      assert.equal(timestampApplications.length, 0);
      assert.equal(requests.length, 0);
      break;
    }
    case 'flow-network':
    case 'flow-http':
    case 'flow-body':
    case 'flow-missing-region':
    case 'flow-missing-csrf':
    case 'flow-missing-state':
    case 'flow-missing-input':
    case 'flow-invalid-report':
    case 'flow-duplicate-report': {
      const kind = process.argv[1].slice(5);
      if (kind === 'missing-region') delete invalid.nodes.actions;
      if (kind === 'missing-csrf') delete invalid.body.dataset.csrf;
      if (kind === 'missing-state') delete invalid.nodes.state;
      if (kind === 'missing-input') invalid.nodes['task-form'].children = [];
      if (kind === 'invalid-report') invalid.panel.children = [];
      if (kind === 'duplicate-report')
        invalid.nodes.reports.appendChild(new Element('duplicate', [new Element('pre')], {report:'qa'}));
      const fail = async () => {
        if (kind === 'network') throw Error('offline');
        return {
          ok:kind !== 'http',
          text:async () => {
            if (kind === 'body') throw Error('body interrupted');
            return 'invalid';
          },
        };
      };
      document.input.value = '保留輸入 draft';
      document.input.selectionStart = 2; document.input.selectionEnd = 5;
      document.activeElement = document.input;
      document.panel.open = true;
      document.pre.scrollTop = 70; document.pre.scrollLeft = 5;
      document.action.dataset.action = 'confirm-delivery';
      const form = document.nodes['task-form'];
      form.hidden = false;
      next.nodes['task-form'].hidden = true;
      stale.nodes['task-form'].hidden = true;
      const preserved = () => {
        assert.equal(document.nodes['task-form'], form);
        assert.equal(form.hidden, false);
        assert.equal(form.querySelector('textarea'), document.input);
        assert.equal(document.input.value, '保留輸入 draft');
        assert.equal(document.activeElement, document.input);
        assert.equal(document.input.selectionStart, 2);
        assert.equal(document.input.selectionEnd, 5);
        assert.equal(document.nodes.reports.children[0], document.panel);
        assert.equal(document.panel.open, true);
        assert.equal(document.pre.scrollTop, 70);
        assert.equal(document.pre.scrollLeft, 5);
        assert.equal(document.nodes.git.innerHTML, 'impact-old');
        assert.equal(document.nodes.git.dataset.snapshotId, 'old');
        assert.equal(document.nodes.actions.innerHTML, 'actions-old');
        assert.equal(document.nodes.actions.children[0], document.action);
        assert.equal(document.action.dataset.action, 'confirm-delivery');
        assert.equal(run('deliverySnapshotId'), 'old');
        assert.equal(window.scrollX, 20); assert.equal(window.scrollY, 400);
      };
      const screen = () => [
        ...['workflow-status', 'tasks', 'task-form', 'actions', 'git', 'reports']
          .map(id => document.nodes[id].innerHTML),
        document.pre.innerHTML, run('csrf'), run('deliverySnapshotId'),
      ];
      const assertFailure = async (lastTimestamp, applications) => {
        const before = screen();
        const mutationCount = mutations.length;
        clock += 60000;
        request = fail;
        await run('refresh()');
        assert.equal(document.nodes['refresh-error'].textContent, '更新失敗，正在顯示上次資料');
        assert.equal(document.nodes['last-refresh'].textContent, lastTimestamp);
        assert.equal(timestampApplications.length, applications);
        assert.deepEqual(screen(), before);
        assert.equal(mutations.length, mutationCount);
        preserved();
      };
      assert.equal(document.nodes['last-refresh'].textContent, '');
      await assertFailure('', 0);
      request = async () => response;
      clock += 60000;
      await run('refresh()');
      const firstTimestamp = timestamp();
      assert.equal(document.nodes['last-refresh'].textContent, firstTimestamp);
      assert.equal(document.nodes['refresh-error'].textContent, '');
      assert.equal(document.nodes.tasks.innerHTML, 'tasks-new');
      assert.equal(document.pre.innerHTML, 'report-new');
      assert.equal(run('csrf'), 'csrf-new');
      assert.deepEqual(timestampApplications, ['phase-new']);
      preserved();
      await assertFailure(firstTimestamp, 1);
      clock += 60000;
      request = async () => ({ok:true, text:async () => 'stale'});
      await run('refresh()');
      assert.equal(document.nodes['refresh-error'].textContent, '');
      assert.equal(document.nodes['last-refresh'].textContent, timestamp());
      assert.notEqual(document.nodes['last-refresh'].textContent, firstTimestamp);
      assert.equal(document.nodes.tasks.innerHTML, 'tasks-stale');
      assert.equal(document.pre.innerHTML, 'report-stale');
      assert.equal(run('csrf'), 'csrf-stale');
      assert.deepEqual(timestampApplications, ['phase-new', 'phase-stale']);
      preserved();
      break;
    }
    case 'status-deferred': {
      request = async () => { throw Error('offline'); };
      await run('refresh()');
      document.activeElement = document.nodes['workflow-status'];
      request = async () => response;
      await run('refresh()');
      assert.equal(document.nodes['workflow-status'].innerHTML, 'phase-old');
      assert.equal(document.nodes['last-refresh'].textContent, '');
      assert.equal(document.nodes['refresh-error'].textContent, '更新失敗，正在顯示上次資料');
      assert.equal(timestampApplications.length, 0);
      document.activeElement = document.body;
      await run('refresh()');
      assert.equal(document.nodes['workflow-status'].innerHTML, 'phase-new');
      assert.equal(document.nodes['last-refresh'].textContent, timestamp());
      assert.equal(document.nodes['refresh-error'].textContent, '');
      assert.deepEqual(timestampApplications, ['phase-new']);
      break;
    }
    case 'preserve': {
      document.input.value = '繁體中文 draft';
      document.input.selectionStart = 2; document.input.selectionEnd = 4;
      document.activeElement = document.input;
      document.panel.open = true; document.pre.scrollTop = 70; document.pre.scrollLeft = 5;
      await run('refresh()'); await run('refresh()');
      assert.equal(document.input.value, '繁體中文 draft');
      assert.equal(document.activeElement, document.input);
      assert.equal(document.input.selectionStart, 2); assert.equal(document.input.selectionEnd, 4);
      assert.equal(document.nodes.reports.children[0], document.panel);
      assert.equal(document.panel.open, true); assert.equal(document.pre.scrollTop, 70);
      assert.equal(document.pre.scrollLeft, 5); assert.equal(document.pre.innerHTML, 'report-new');
      assert.equal(window.scrollY, 400); assert.equal(document.nodes.tasks.innerHTML, 'tasks-new');
      break;
    }
    case 'delivery': {
      document.activeElement = document.action;
      await run('refresh()');
      assert.equal(document.activeElement, document.action);
      assert.equal(document.nodes.actions.innerHTML, 'actions-old');
      assert.equal(run('deliverySnapshotId'), 'new');
      request = async () => response;
      await run("post('/api/deliver', {confirm:true,snapshot_id:deliverySnapshotId})");
      const post = requests.find(item=>item.options.method==='POST');
      assert.equal(JSON.parse(post.options.body).snapshot_id, 'new');
      assert.equal(post.options.headers['X-CSRF-Token'], 'csrf-new');
      break;
    }
    case 'selection': {
      selection = {isCollapsed:false, anchorNode:document.nodes.git, focusNode:document.nodes.git};
      await run('refresh()');
      assert.equal(document.nodes.git.innerHTML, 'impact-old');
      assert.equal(document.nodes.actions.innerHTML, 'actions-old');
      assert.equal(run('deliverySnapshotId'), 'old');
      selection = {isCollapsed:true};
      await run('refresh()');
      assert.equal(document.nodes.git.innerHTML, 'impact-new');
      assert.equal(run('deliverySnapshotId'), 'new');
      break;
    }
    case 'report-selection': {
      selection = {isCollapsed:false, anchorNode:document.pre, focusNode:document.pre};
      document.panel.open = true;
      await run('refresh()');
      assert.equal(document.pre.innerHTML, 'report-old');
      assert.equal(document.panel.open, true);
      selection = {isCollapsed:true};
      await run('refresh()');
      assert.equal(document.pre.innerHTML, 'report-new');
      break;
    }
    case 'report-membership': {
      const added = new Element('added', [new Element('added report')], {report:'new-report'});
      const removed = new Element('removed', [new Element('old report')], {report:'obsolete'});
      next.nodes.reports.appendChild(added);
      document.nodes.reports.appendChild(removed);
      await run('refresh()');
      assert.deepEqual(document.nodes.reports.children.map(node=>node.dataset.report), ['qa', 'new-report']);
      assert.equal(document.nodes.reports.children[0], document.panel);
      break;
    }
    case 'incomplete': {
      delete next.nodes.actions;
      await run('refresh()');
      assert.equal(document.nodes.tasks.innerHTML, 'tasks-old');
      assert.equal(run('deliverySnapshotId'), 'old');
      assert.ok(document.nodes['refresh-error'].textContent);
      break;
    }
    case 'failure': {
      document.input.value = 'keep draft';
      request = async (url, options) => options.method==='POST' ?
        {ok:false,json:async()=>({error:'Rejected'})} : response;
      await run("post('/api/task', {prompt:'keep draft'})");
      assert.equal(document.input.value, 'keep draft');
      assert.deepEqual(alerts, ['Rejected']);
      assert.equal(document.submit.disabled, false);
      request = async () => { throw Error('disconnected'); };
      await run('refresh()');
      assert.ok(document.nodes['refresh-error'].textContent);
      assert.equal(document.nodes.tasks.innerHTML, 'tasks-new');
      request = async () => response;
      await run('refresh()');
      assert.equal(document.nodes['refresh-error'].textContent, '');
      break;
    }
    case 'race': {
      let release;
      request = (url, options) => options.method==='POST' ? Promise.resolve(response) :
        new Promise(resolve=>release=resolve);
      const poll = run('refresh()');
      const action = run("post('/api/continue')");
      await run("post('/api/continue')");
      assert.equal(requests.filter(item=>item.options.method==='POST').length, 1);
      assert.equal(document.submit.disabled, true);
      await Promise.resolve(); await Promise.resolve();
      request = async () => response;
      release({ok:true, text:async()=> 'stale'});
      await Promise.all([poll, action]);
      assert.equal(requests.filter(item=>!item.options.method).length, 2);
      assert.equal(document.nodes.tasks.innerHTML, 'tasks-new');
      assert.ok(!mutations.includes('tasks-stale'));
      assert.equal(document.submit.disabled, false);
      break;
    }
    case 'submit': {
      document.input.value = 'submitted';
      request = async () => response;
      await run("post('/api/task', {prompt:'submitted'})");
      assert.equal(document.input.value, '');
      document.input.value = 'newer draft';
      await run("post('/api/task', {prompt:'submitted'})");
      assert.equal(document.input.value, 'newer draft');
      break;
    }
    default: throw Error('Unknown test case');
  }
  assert.equal(timers[0].delay, 3000);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


@pytest.mark.parametrize(
    "case",
    [
        "initial",
        "flow-network",
        "flow-http",
        "flow-body",
        "flow-missing-region",
        "flow-missing-csrf",
        "flow-missing-state",
        "flow-missing-input",
        "flow-invalid-report",
        "flow-duplicate-report",
        "status-deferred",
        "preserve",
        "delivery",
        "selection",
        "report-selection",
        "report-membership",
        "incomplete",
        "failure",
        "race",
        "submit",
    ],
)
def test_gui_refresh_behavior(case):
    """Run the production JavaScript offline against a simulated DOM."""
    node = shutil.which("node")
    if not node:
        pytest.skip(
            "Node.js is needed to execute the GUI script; no npm packages required"
        )
    page = render_page({"csrf": "csrf-old", "delivery_snapshot_id": "old"})
    script = re.search(r"<script>(.*?)</script>", page, re.DOTALL).group(1)
    result = subprocess.run(
        [node, "-e", HARNESS, case],
        input=script,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("node_id", ["last-refresh", "refresh-error"])
def test_gui_refresh_initial_markup_has_empty_accessible_status(node_id):
    """Inspect rendered HTML without treating the DOM double as an HTML parser."""
    page = render_page({"csrf": "csrf-old", "delivery_snapshot_id": "old"})
    assert re.search(
        rf'<p\b(?=[^>]*\bid="{node_id}")(?=[^>]*\brole="status")[^>]*>\s*</p>',
        page,
    )
