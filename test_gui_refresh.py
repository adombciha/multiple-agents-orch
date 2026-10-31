"""Execute the rendered GUI script against a small DOM double, without dependencies."""

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
let selection = {isCollapsed:true};
const alerts = [], timers = [], requests = [];
const window = {scrollX:20, scrollY:400,
  getSelection:() => selection,
  scrollTo(x,y) { this.scrollX=x; this.scrollY=y; },
};
let request = async () => ({ok:true, text:async()=> 'new'});
const context = vm.createContext({document, window,
  DOMParser:class { parseFromString(key) { return key === 'stale' ? stale : next; } },
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
      assert.equal(document.activeElement, next.action);
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
