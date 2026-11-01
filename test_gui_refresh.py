"""Offline simulated DOM tests executing the rendered GUI script with Node.js.

No npm packages, network access, or browser dependencies are used. The stdlib
HTMLParser only builds fixtures from render_page; it does not validate browser
HTML parsing. Real-browser acceptance must still cover HTML parsing, native
focus, Range/selection, keyboard interaction, and responsive layout/scrolling.
Passing this harness is not visual acceptance.
"""

import copy
import hashlib
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

from orchestrator.gui import render_page
from test_gui import OWNER_REVIEW_CASES, owner_status


class RenderedDOM(HTMLParser):
    """Serialize rendered markup as fixtures, without implementing GUI decisions."""

    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "document", "attrs": {}, "children": []}
        self.stack = [self.root]
        self.feed(page)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "children": []}
        self.stack[-1]["children"].append(node)
        if tag not in {"meta", "link", "input", "br", "hr", "img"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1]["children"].append(data)


HARNESS = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const payload = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const fragments = new Map(), mutations = [], textMutations = [];
const escape = text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
class Text {
  constructor(text) { this.textContent = text; this.parent = null; }
  contains(node) { return this === node; }
  get outerHTML() { return escape(this.textContent); }
}
function build(spec) { return typeof spec === 'string' ? new Text(spec) : new Element(spec); }
function specOf(node) {
  if (node instanceof Text) return node.textContent;
  const attrs = Object.fromEntries(Object.entries(node.attrs).filter(([key]) => !key.startsWith('data-')));
  attrs.class = node.className;
  for (const [key, value] of Object.entries(node.dataset))
    attrs['data-' + key.replace(/[A-Z]/g, c => '-' + c.toLowerCase())] = value;
  for (const key of ['hidden', 'disabled', 'open']) {
    if (node[key]) attrs[key] = '';
    else delete attrs[key];
  }
  return {tag:node.tag, attrs, children:node.childNodes.map(specOf)};
}
class Element {
  constructor(spec) {
    this.tag = spec.tag; this.attrs = {...spec.attrs}; this.dataset = {};
    for (const [key, value] of Object.entries(this.attrs))
      if (key.startsWith('data-'))
        this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
    this.className = this.attrs.class || '';
    this.disabled = 'disabled' in this.attrs; this.hidden = 'hidden' in this.attrs;
    this.open = 'open' in this.attrs;
    this.scrollTop = 0; this.scrollLeft = 0;
    this.value = ''; this.selectionStart = 0; this.selectionEnd = 0;
    this.parent = null;
    this.childNodes = spec.children.map(build);
    this.childNodes.forEach(child => child.parent = this);
    fragments.set(this.innerHTML, spec.children);
  }
  get children() { return this.childNodes.filter(node => node instanceof Element); }
  get firstElementChild() { return this.children[0] || null; }
  get nextElementSibling() {
    const siblings = this.parent.children;
    return siblings[siblings.indexOf(this) + 1] || null;
  }
  get innerHTML() { return this.childNodes.map(node => node.outerHTML).join(''); }
  set innerHTML(value) {
    mutations.push(value);
    const specs = fragments.get(value);
    assert.ok(specs || !value.includes('<'), 'Unregistered HTML fixture');
    this.childNodes.forEach(child => child.parent = null);
    this.childNodes = (specs || [value]).map(build);
    this.childNodes.forEach(child => child.parent = this);
  }
  get outerHTML() {
    const spec = specOf(this);
    const attrs = Object.entries(spec.attrs).map(([key, value]) =>
      ' ' + key + '="' + escape(value || '') + '"').join('');
    const start = '<' + this.tag + attrs + '>';
    return ['meta','link','input','br','hr','img'].includes(this.tag) ? start :
      start + this.innerHTML + '</' + this.tag + '>';
  }
  get textContent() { return this.childNodes.map(node => node.textContent).join(''); }
  set textContent(value) {
    textMutations.push({node:this, value:String(value)});
    this.childNodes.forEach(child => child.parent = null);
    this.childNodes = [new Text(String(value))]; this.childNodes[0].parent = this;
  }
  contains(node) { return this === node || this.childNodes.some(child => child.contains(node)); }
  matches(selector) {
    const match = /^([a-z][a-z0-9-]*)?(?:#([\w-]+))?((?:\[[^\]]+\])*)$/.exec(selector);
    assert.ok(match, 'Unsupported selector: ' + selector);
    if (match[1] && this.tag !== match[1]) return false;
    if (match[2] && this.attrs.id !== match[2]) return false;
    return Array.from(match[3].matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)).every(([,key,value]) => {
      const actual = key.startsWith('data-') ?
        this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] : this.attrs[key];
      return actual !== undefined && (value === undefined || actual === value);
    });
  }
  querySelectorAll(selector) {
    const parts = selector.split(' ');
    const result = [];
    for (const child of this.children) {
      if (child.matches(parts[0])) {
        if (parts.length === 1) result.push(child);
        else result.push(...child.querySelectorAll(parts.slice(1).join(' ')));
      }
      result.push(...child.querySelectorAll(selector));
    }
    return Array.from(new Set(result));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  appendChild(child) { return this.insertBefore(child, null); }
  insertBefore(child, before) {
    if (child === before) return child;
    if (child.parent) child.remove();
    const index = before ? this.childNodes.indexOf(before) : this.childNodes.length;
    assert.ok(index >= 0);
    this.childNodes.splice(index, 0, child); child.parent = this;
    return child;
  }
  remove() {
    if (this.parent) {
      this.parent.childNodes = this.parent.childNodes.filter(child => child !== this);
      this.parent = null;
    }
  }
  focus() { document.activeElement = this; }
}
function page(spec) {
  const root = build(spec), body = root.querySelector('body');
  const doc = {
    root, body, activeElement:body,
    getElementById(id) { return root.querySelector('#' + id); },
    querySelector(selector) { return root.querySelector(selector); },
    querySelectorAll(selector) { return root.querySelectorAll(selector); },
    contains(node) { return root.contains(node); },
    listeners:new Map(),
    addEventListener(type, callback) {
      if (!this.listeners.has(type)) this.listeners.set(type, []);
      this.listeners.get(type).push(callback);
    },
  };
  doc.nodes = new Proxy({}, {get:(_, id) => doc.getElementById(id)});
  doc.input = doc.querySelector('#task-form textarea');
  doc.submit = doc.querySelector('#task-form button');
  doc.action = doc.querySelector('[data-action="confirm-delivery"]') ||
    doc.querySelector('#actions button') || doc.querySelector('#delivery-actions button');
  doc.panel = doc.querySelector('[data-report="qa"]');
  doc.pre = doc.panel ? doc.panel.querySelector('pre') : null;
  return doc;
}
const document = page(payload.pages.old), next = page(payload.pages.new);
const stale = page(payload.pages.stale), invalid = page(payload.pages.invalid);
const snapshots = Object.fromEntries(Object.entries(payload.pages)
  .map(([key, spec]) => [key, page(spec)]));
let clock = Date.UTC(2026, 0, 2, 3, 4, 5);
const timestampApplications = [];
class LocalDate extends Date {
  constructor(...args) { super(...(args.length ? args : [clock])); }
  toLocaleString(...args) {
    timestampApplications.push(document.nodes['workflow-status'].textContent);
    return super.toLocaleString(...args);
  }
}
const timestamp = () => '最後成功更新：' + new Date(clock).toLocaleString();
let selection = {isCollapsed:true};
const alerts = [], timers = [], requests = [];
const window = {scrollX:20, scrollY:400,
  getSelection:() => selection,
  confirm:() => true,
  scrollTo(x,y) { this.scrollX=x; this.scrollY=y; },
};
const response = (key='new', data={}) => ({
  ok:true, json:async()=>data, text:async()=>key,
});
let request = async () => response();
const context = vm.createContext({document, window, Date:LocalDate,
  DOMParser:class { parseFromString(key, type) {
    assert.equal(type, 'text/html');
    if (key === 'parse-error') throw Error('parse failed');
    const doc = {new:next, stale, invalid}[key] || snapshots[key];
    assert.ok(doc, 'Unknown response fixture: ' + key);
    return page(specOf(doc.root));
  } },
  fetch:(url, options) => { requests.push({url, options}); return request(url, options); },
  setTimeout:(callback,delay)=>timers.push({callback,delay}),
  alert:message=>alerts.push(message),
  FormData:class {
    constructor(form) { this.form = form; }
    get(name) { assert.equal(name, 'prompt'); return this.form.querySelector('textarea').value; }
  },
  location:{reload() { throw Error('A full page reload must never occur'); }},
});
vm.runInContext(payload.script, context);
const run = expression => vm.runInContext(expression, context);
const posts = () => requests.filter(item => item.options.method === 'POST');
const buttons = () => document.querySelectorAll('button[data-action]');
const confirmButton = () => document.querySelector('[data-action="confirm-delivery"]');
const displayButton = () => document.querySelector('[data-action="deliver{}"]');
const feedback = id => document.nodes[id].textContent;
const readingNotice = '資料已更新，目前保留閱讀內容';
const dispatch = type => {
  const callbacks = document.listeners.get(type) || [];
  assert.ok(callbacks.length, 'Production listener missing: ' + type);
  callbacks.forEach(callback => callback());
};
const closeReading = node => {
  node.open = false;
  node.querySelectorAll('details[data-detail]').forEach(detail => detail.open = false);
};
const assertNotice = (id, region) => {
  const notice = document.nodes[id];
  assert.equal(notice.textContent, readingNotice);
  assert.equal(notice.attrs.role, 'status');
  assert.equal(notice.hidden, false);
  assert.equal(region.contains(notice), false);
};
const assertConfirmation = () => {
  const button = confirmButton(), preview = document.nodes.git.querySelector('[data-preview]');
  assert.ok(button && preview.contains(button));
  assert.ok(document.nodes['delivery-actions'].contains(button));
  assert.equal(button.dataset.snapshotId, document.nodes.git.dataset.snapshotId);
  let node = button, expanded = null;
  while (node) {
    assert.equal(node.hidden, false);
    if (node.tag === 'details') {
      assert.equal(node.open, true);
      expanded = node;
    }
    node = node.parent;
  }
  assert.ok(expanded && expanded.querySelector('pre'));
  assert.equal(button.disabled, false);
  assert.equal(button.className, 'primary');
};
const allDisabled = () => assert.ok(buttons().every(node => node.disabled));
const currentPhase = version => assert.ok(document.nodes['workflow-status'].textContent.includes('phase-' + version));
const currentTasks = version => assert.ok(document.nodes.tasks.textContent.includes('description-' + version));
const currentReport = version => assert.ok(document.pre.textContent.includes('qa-' + version));
const oldPreview = () => {
  assert.equal(document.nodes.git.dataset.snapshotId, 'old');
  assert.equal(run('deliverySnapshotId'), 'old');
  assert.ok(document.nodes.git.querySelector('[data-preview]').textContent.includes('impact-old'));
};
const invalidConfirmation = () => {
  assert.ok(confirmButton().disabled);
  assert.equal(confirmButton().className, 'secondary');
  assert.match(feedback('delivery-feedback'), /重新展示/);
};
const assertDisplayDecision = (title, explanation) => {
  const copy = document.nodes['decision-copy'];
  assert.equal(copy.querySelector('h3').textContent, title);
  assert.match(copy.querySelector('p').textContent, explanation);
  assert.equal(feedback('decision-status'), '');
  assert.equal(displayButton().disabled, false);
  assert.equal(confirmButton().disabled, true);
  assert.equal(confirmButton().className, 'secondary');
  assert.deepEqual(buttons().filter(node => !node.disabled && node.className === 'primary')
    .map(node => node.dataset.action), ['deliver{}']);
};
const defer = () => {
  let resolve;
  const promise = new Promise(done => resolve = done);
  return {promise, resolve};
};
const click = async node => {
  if (!node || node.disabled) return;
  await run(node.attrs.onclick);
};
const submit = async () => {
  let prevented = false;
  await document.nodes['task-form'].onsubmit({
    target:document.nodes['task-form'], preventDefault() { prevented = true; },
  });
  assert.ok(prevented);
};
function assertApplied(count) {
  assert.equal(feedback('last-refresh'), timestamp());
  assert.equal(feedback('refresh-error'), '');
  assert.equal(feedback('connection-status'), '連線正常');
  assert.equal(timestampApplications.length, count);
}
function damage(doc, kind) {
  if (kind === 'missing-region') doc.nodes.actions.remove();
  else if (kind.startsWith('region:')) doc.nodes[kind.slice(7)].remove();
  else if (kind === 'missing-csrf') delete doc.body.dataset.csrf;
  else if (kind === 'empty-csrf') doc.body.dataset.csrf = '';
  else if (kind === 'missing-state') doc.nodes.state.remove();
  else if (kind === 'missing-input') doc.input.remove();
  else if (kind === 'invalid-report') doc.panel.querySelector('pre').remove();
  else if (kind === 'missing-summary') doc.panel.querySelector('summary').remove();
  else if (kind === 'missing-preview') doc.nodes.git.querySelector('[data-preview]').remove();
  else if (kind === 'duplicate-report') doc.nodes.reports.appendChild(build(specOf(doc.panel)));
  else if (kind === 'duplicate-task') doc.nodes.tasks.appendChild(build(specOf(doc.nodes.tasks.children[0])));
  else if (kind === 'missing-task-key') delete doc.nodes.tasks.children[0].dataset.task;
  else if (kind === 'missing-report-key') delete doc.panel.dataset.report;
  else if (kind === 'invalid-enabled') doc.submit.dataset.enabled = 'yes';
  else if (kind === 'duplicate-control') doc.nodes.actions.appendChild(build(specOf(doc.submit)));
}
function failResponse(kind) {
  return async () => {
    if (kind === 'network') throw Error('offline');
    return {ok:kind !== 'http', text:async () => {
      if (kind === 'body') throw Error('body interrupted');
      return kind === 'parse' ? 'parse-error' : 'invalid';
    }};
  };
}
(async () => {
  const testCase = process.argv[1];
  if (testCase.startsWith('flow-')) {
    const kind = testCase.slice(5);
    damage(invalid, kind);
    document.input.value = '保留輸入 draft';
    document.input.selectionStart = 2; document.input.selectionEnd = 5;
    document.activeElement = document.input;
    document.panel.open = true;
    document.pre.scrollTop = 70; document.pre.scrollLeft = 5;
    const form = document.nodes['task-form'], action = document.action;
    const preview = document.nodes.git.querySelector('[data-preview]');
    const previewText = preview.querySelector('pre'), previewHTML = previewText.innerHTML;
    form.hidden = false;
    const preserved = () => {
      assert.equal(document.nodes['task-form'], form); assert.equal(form.hidden, false);
      assert.equal(form.querySelector('textarea'), document.input);
      assert.equal(document.input.value, '保留輸入 draft');
      assert.equal(document.activeElement, document.input);
      assert.equal(document.input.selectionStart, 2); assert.equal(document.input.selectionEnd, 5);
      assert.equal(document.nodes.reports.children[0], document.panel);
      assert.equal(document.panel.open, true);
      assert.equal(document.pre.scrollTop, 70); assert.equal(document.pre.scrollLeft, 5);
      assert.equal(document.nodes.git.querySelector('[data-preview]'), preview);
      assert.equal(preview.querySelector('pre'), previewText);
      assert.equal(previewText.innerHTML, previewHTML);
      assert.equal(confirmButton(), action);
      assert.equal(action.dataset.action, 'confirm-delivery');
      oldPreview();
      assert.equal(window.scrollX, 20); assert.equal(window.scrollY, 400);
    };
    const screen = () => [
      ...['workflow-status','tasks','task-form','actions','git','reports']
        .map(id => document.nodes[id].textContent),
      document.pre.innerHTML, run('csrf'), run('deliverySnapshotId'),
    ];
    const assertFailure = async (lastTimestamp, applications) => {
      const before = screen(), mutationCount = mutations.length;
      clock += 60000; request = failResponse(kind);
      await run('refresh()');
      assert.equal(feedback('refresh-error'), '更新失敗，正在顯示上次資料');
      assert.equal(feedback('connection-status'), '連線異常，操作暫停');
      assert.equal(feedback('last-refresh'), lastTimestamp);
      assert.equal(timestampApplications.length, applications);
      assert.deepEqual(screen(), before); assert.equal(mutations.length, mutationCount);
      allDisabled(); preserved();
      const count = posts().length;
      await click(action);
      await run("post('/api/deliver', {confirm:true,snapshot_id:deliverySnapshotId})");
      await submit();
      assert.equal(posts().length, count);
    };
    assert.equal(feedback('last-refresh'), '');
    await assertFailure('', 0);
    request = async () => response(); clock += 60000;
    await run('refresh()');
    const firstTimestamp = timestamp();
    assertApplied(1); currentTasks('new'); currentReport('old');
    assertNotice('reports-reading-status', document.nodes.reports);
    assert.equal(run('csrf'), 'csrf-new');
    assert.ok(timestampApplications[0].includes('phase-new'));
    preserved(); invalidConfirmation();
    assert.ok(document.submit.disabled);
    await assertFailure(firstTimestamp, 1);
    clock += 60000; request = async () => response('stale');
    await run('refresh()');
    assertApplied(2);
    assert.notEqual(feedback('last-refresh'), firstTimestamp);
    currentTasks('stale'); currentReport('old');
    assertNotice('reports-reading-status', document.nodes.reports);
    assert.equal(run('csrf'), 'csrf-stale');
    assert.ok(timestampApplications[1].includes('phase-stale'));
    preserved(); invalidConfirmation();
  } else switch (testCase) {
    case 'initial': {
      assert.equal(feedback('last-refresh'), '');
      assert.equal(feedback('refresh-error'), '');
      assert.equal(feedback('connection-status'), '已載入，等待背景更新');
      assert.equal(feedback('action-feedback'), '');
      assert.equal(feedback('delivery-feedback'), '');
      assert.equal(timestampApplications.length, 0); assert.equal(requests.length, 0);
      break;
    }
    case 'status-deferred': {
      request = failResponse('network'); await run('refresh()');
      document.activeElement = document.nodes['workflow-status'];
      request = async () => response(); await run('refresh()');
      currentPhase('new'); assertApplied(1);
      assert.equal(document.activeElement, document.nodes['workflow-status']);
      assert.equal(document.submit.disabled, false);
      assert.ok(timestampApplications[0].includes('phase-new'));
      document.activeElement = document.body; clock += 60000; await run('refresh()');
      currentPhase('new'); assertApplied(2);
      break;
    }
    case 'preserve': {
      document.input.value = '繁體中文 draft';
      document.input.selectionStart = 2; document.input.selectionEnd = 4;
      document.activeElement = document.input;
      document.panel.open = true; document.pre.scrollTop = 70; document.pre.scrollLeft = 5;
      const details = document.querySelectorAll('details[data-detail]');
      details.forEach((node, index) => node.open = index % 2 === 0);
      const open = new Map(details.map(node => [node.dataset.detail, node.open]));
      for (const [index, node] of document.querySelectorAll('pre').entries()) {
        node.scrollTop = 70 + index; node.scrollLeft = 5 + index;
      }
      document.pre.scrollTop = 70; document.pre.scrollLeft = 5;
      const scrolls = new Map(details.map(node => [node.dataset.detail,
        [node.querySelector('pre').scrollTop, node.querySelector('pre').scrollLeft]]));
      document.nodes.tasks.scrollTop = 90; document.nodes.tasks.scrollLeft = 7;
      document.nodes.reports.scrollTop = 100; document.nodes.reports.scrollLeft = 8;
      const reportNodes = new Map(document.nodes.reports.children.map(node => [node.dataset.report, node]));
      await run('refresh()'); clock += 60000; await run('refresh()');
      assert.equal(document.input.value, '繁體中文 draft');
      assert.equal(document.activeElement, document.input);
      assert.equal(document.input.selectionStart, 2); assert.equal(document.input.selectionEnd, 4);
      assert.equal(document.nodes.reports.children[0], document.panel);
      document.querySelectorAll('details[data-detail]').forEach(node => {
        assert.equal(node.open, open.get(node.dataset.detail), node.dataset.detail);
        assert.deepEqual([node.querySelector('pre').scrollTop, node.querySelector('pre').scrollLeft],
          scrolls.get(node.dataset.detail), node.dataset.detail);
      });
      for (const node of document.nodes.reports.children) {
        assert.equal(node, reportNodes.get(node.dataset.report));
        assert.ok(node.querySelector('pre').textContent.includes(node.dataset.report + '-old'));
      }
      assert.equal(document.panel.open, open.get('report-qa'));
      assert.equal(document.pre.scrollTop, 70); assert.equal(document.pre.scrollLeft, 5);
      assert.equal(document.nodes.tasks.scrollTop, 90); assert.equal(document.nodes.tasks.scrollLeft, 7);
      assert.equal(document.nodes.reports.scrollTop, 100); assert.equal(document.nodes.reports.scrollLeft, 8);
      assert.equal(window.scrollX, 20); assert.equal(window.scrollY, 400);
      currentReport('old'); currentTasks('old'); assertApplied(2);
      assertNotice('tasks-reading-status', document.nodes.tasks);
      assertNotice('reports-reading-status', document.nodes.reports);
      closeReading(document.nodes.tasks); closeReading(document.nodes.reports);
      dispatch('toggle');
      currentReport('new'); currentTasks('new');
      assert.equal(feedback('tasks-reading-status'), '');
      assert.equal(feedback('reports-reading-status'), '');
      assert.equal(document.activeElement, document.input);
      assert.equal(document.input.value, '繁體中文 draft');
      assert.equal(window.scrollX, 20); assert.equal(window.scrollY, 400);
      break;
    }
    case 'delivery': {
      closeReading(document.nodes.git);
      assert.equal(document.nodes.git.querySelector('details').open, false);
      document.activeElement = document.action;
      const actionsHTML = document.nodes.actions.innerHTML;
      await run('refresh()');
      assert.equal(document.activeElement, document.action);
      assert.equal(document.nodes.actions.innerHTML, actionsHTML);
      assert.equal(run('deliverySnapshotId'), 'new');
      assert.ok(document.nodes.git.querySelector('[data-preview]').textContent.includes('impact-new'));
      assert.equal(confirmButton().dataset.snapshotId, 'new');
      assertConfirmation();
      assert.equal(document.nodes['decision-copy'].textContent, next.nodes['decision-copy'].textContent);
      assert.equal(document.nodes['decision-copy'].querySelector('h3').textContent,
        '已展示 Git 影響，等待二次確認');
      assert.deepEqual(buttons().filter(node => !node.disabled && node.className === 'primary')
        .map(node => node.dataset.action), ['confirm-delivery']);
      assert.equal(posts().length, 0);
      await click(confirmButton());
      assert.equal(posts().length, 1);
      assert.equal(JSON.parse(posts()[0].options.body).snapshot_id, 'new');
      assert.equal(posts()[0].options.headers['X-CSRF-Token'], 'csrf-new');
      break;
    }
    case 'selection': {
      const preview = document.nodes.git.querySelector('[data-preview]');
      const text = preview.querySelector('pre'), html = text.innerHTML;
      const actions = document.nodes.actions.innerHTML;
      selection = {isCollapsed:false, anchorNode:preview, focusNode:preview};
      const selected = selection;
      await run('refresh()');
      oldPreview(); assert.equal(text.innerHTML, html);
      assert.equal(document.nodes.actions.innerHTML, actions);
      assert.equal(selection, selected); assert.equal(confirmButton(), null);
      selection = {isCollapsed:true}; await run('refresh()');
      oldPreview(); assertNotice('git-reading-status', preview);
      closeReading(preview); dispatch('toggle');
      assert.ok(preview.textContent.includes('impact-new'));
      assertConfirmation();
      assert.equal(run('deliverySnapshotId'), 'new');
      break;
    }
    case 'report-selection': {
      selection = {isCollapsed:false, anchorNode:document.pre.childNodes[0], focusNode:document.pre.childNodes[0]};
      const selected = selection;
      document.panel.open = true;
      await run('refresh()');
      currentReport('old'); assert.equal(document.panel.open, true);
      assert.equal(selection, selected);
      selection = {isCollapsed:true}; await run('refresh()'); currentReport('old');
      assertNotice('reports-reading-status', document.nodes.reports);
      document.panel.open = false; dispatch('toggle'); currentReport('new');
      break;
    }
    case 'report-membership': {
      const added = build(specOf(next.panel)), removed = build(specOf(document.panel));
      added.dataset.report = 'new-report'; added.dataset.detail = 'report-new-report';
      added.querySelector('pre').textContent = 'added report';
      removed.dataset.report = 'obsolete'; removed.dataset.detail = 'report-obsolete';
      next.nodes.reports.appendChild(added); document.nodes.reports.appendChild(removed);
      next.nodes.reports.insertBefore(next.nodes.reports.children[1], next.panel);
      const wanted = next.nodes.reports.children.map(node => node.dataset.report);
      await run('refresh()');
      assert.deepEqual(document.nodes.reports.children.map(node=>node.dataset.report), wanted);
      assert.equal(document.querySelector('[data-report="qa"]'), document.panel);
      assert.equal(removed.parent, null);
      assert.equal(document.querySelector('[data-report="new-report"]').querySelector('pre').textContent, 'added report');
      break;
    }
    case 'incomplete': {
      next.nodes.actions.remove(); await run('refresh()');
      currentTasks('old'); oldPreview();
      assert.ok(feedback('refresh-error')); allDisabled();
      break;
    }
    case 'failure': {
      document.input.value = 'keep draft';
      request = async (url, options) => options.method === 'POST' ?
        {ok:false,json:async()=>({error:'Rejected'})} : response();
      await run("post('/api/task', {prompt:'keep draft'})");
      assert.equal(document.input.value, 'keep draft'); assert.deepEqual(alerts, ['Rejected']);
      assert.equal(document.submit.disabled, false);
      assert.match(feedback('action-feedback'), /操作未完成：Rejected/);
      request = failResponse('network'); await run('refresh()');
      assert.ok(feedback('refresh-error')); currentTasks('new'); allDisabled();
      request = async () => response(); await run('refresh()');
      assert.equal(feedback('refresh-error'), ''); assert.equal(document.submit.disabled, false);
      break;
    }
    case 'race': {
      const held = defer();
      request = (url, options) => options.method === 'POST' ? Promise.resolve(response()) : held.promise;
      const poll = run('refresh()'), action = run("post('/api/continue')");
      await run("post('/api/continue')");
      assert.equal(posts().length, 1); assert.equal(document.submit.disabled, true);
      allDisabled();
      await Promise.resolve(); await Promise.resolve();
      request = async () => response();
      if (payload.failure) held.resolve(failResponse(payload.failure)());
      else held.resolve(response('stale'));
      await Promise.all([poll, action]);
      assert.equal(requests.filter(item=>!item.options.method).length, 2);
      currentTasks('new');
      assert.ok(!mutations.some(value => value.includes('description-stale')));
      assert.equal(document.submit.disabled, false); assertApplied(1);
      break;
    }
    case 'submit': {
      document.input.value = 'submitted';
      await submit(); assert.equal(document.input.value, '');
      document.input.value = 'newer draft';
      await run("post('/api/task', {prompt:'submitted'})");
      assert.equal(document.input.value, 'newer draft');
      assert.equal(posts().length, 2);
      assert.equal(JSON.parse(posts()[0].options.body).prompt, 'submitted');
      break;
    }
    case 'polling': {
      const held = defer(); request = () => held.promise;
      const tick = timers.shift(); assert.equal(tick.delay, 3000);
      const first = tick.callback();
      await run('refresh()'); await run('refreshWhenIdle()');
      assert.equal(requests.length, 1); assert.equal(timestampApplications.length, 0);
      held.resolve(response()); await first; assertApplied(1);
      request = failResponse('network');
      await timers.shift().callback();
      assert.equal(feedback('connection-status'), '連線異常，操作暫停');
      assert.equal(timestampApplications.length, 1); allDisabled();
      request = async () => response('stale'); clock += 60000;
      await timers.shift().callback(); currentTasks('stale'); assertApplied(2);
      assert.ok(timers.every(timer => timer.delay === 3000));
      break;
    }
    case 'protected-membership': {
      const removed = document.nodes.reports.children[1];
      next.nodes.reports.children.find(node => node.dataset.report === removed.dataset.report).remove();
      next.nodes.reports.insertBefore(next.nodes.reports.children[2], next.panel);
      selection = {isCollapsed:false, anchorNode:removed.querySelector('pre'), focusNode:document.pre};
      const selected = selection, order = document.nodes.reports.children.slice();
      await run('refresh()');
      assert.equal(selection, selected);
      assert.deepEqual(document.nodes.reports.children, order);
      assert.ok(document.contains(removed)); currentReport('old');
      selection = {isCollapsed:true}; await run('refresh()');
      assert.equal(removed.parent, null); currentReport('new');
      assert.deepEqual(document.nodes.reports.children.map(node => node.dataset.report),
        next.nodes.reports.children.map(node => node.dataset.report));
      break;
    }
    case 'task-membership': {
      const tasks = document.nodes.tasks, original = tasks.children.slice();
      const incoming = next.nodes.tasks;
      incoming.children[1].remove();
      const added = build(specOf(incoming.children[0])); added.dataset.task = 'gamma';
      added.querySelector('details').dataset.detail = 'gamma';
      added.querySelector('pre').textContent = 'new gamma';
      incoming.appendChild(added);
      const details = original[0].querySelector('details');
      details.open = true; details.querySelector('pre').scrollTop = 80;
      await run('refresh()');
      assert.deepEqual(tasks.children, original);
      assert.equal(details.open, true); assert.equal(details.querySelector('pre').scrollTop, 80);
      assertNotice('tasks-reading-status', tasks); assertApplied(1);
      incoming.insertBefore(added, incoming.children[0]);
      document.activeElement = details.querySelector('summary');
      const focused = document.activeElement;
      await run('refresh()');
      assert.equal(document.activeElement, focused); assert.deepEqual(tasks.children, original);
      document.activeElement = document.body; await run('refresh()');
      assert.deepEqual(tasks.children, original);
      details.open = false; dispatch('toggle');
      assert.deepEqual(tasks.children.map(node => node.dataset.task),
        incoming.children.map(node => node.dataset.task));
      assert.equal(tasks.children[0].dataset.task, 'gamma');
      assert.equal(original[0].parent, null); assert.equal(original[1].parent, null);
      assert.ok(tasks.querySelectorAll('details').every(node => !node.open));
      assert.equal(feedback('tasks-reading-status'), '');
      break;
    }
    case 'range-selection': {
      const preview = document.nodes.git.querySelector('[data-preview]');
      const text = preview.querySelector('pre'), html = text.innerHTML;
      selection = {isCollapsed:false, anchorNode:document.nodes['workflow-status'],
        focusNode:document.nodes.reports, rangeCount:1,
        getRangeAt(index) {
          assert.equal(index, 0);
          return {intersectsNode:node => node === preview || node === document.pre};
        }};
      const selected = selection;
      await run('refresh()');
      assert.equal(text.innerHTML, html); oldPreview(); currentReport('old');
      assert.equal(selection, selected); invalidConfirmation();
      break;
    }
    case 'decision-preserved': {
      const copy = document.nodes['decision-copy'], html = copy.innerHTML;
      document.activeElement = copy;
      await run('refresh()');
      assert.equal(copy.innerHTML, html);
      assert.equal(document.activeElement, copy);
      assert.equal(feedback('decision-status'),
        next.nodes['decision-copy'].children.map(node => node.textContent.trim()).join('。'));
      assert.ok(feedback('decision-status'));
      assert.ok(document.submit.disabled);
      assert.ok(!document.contains(document.action) || document.action.disabled);
      const before = posts().length;
      await run("post('/api/continue')"); await submit();
      assert.equal(posts().length, before);
      currentTasks('new'); assertApplied(1);
      document.activeElement = document.body; await run('refresh()');
      assert.equal(copy.textContent, next.nodes['decision-copy'].textContent);
      assert.equal(feedback('decision-status'), '');
      break;
    }
    case 'snapshot-focused':
    case 'snapshot-changed':
    case 'snapshot-invalid':
    case 'snapshot-content-changed': {
      if (testCase === 'snapshot-invalid') {
        next.nodes.git.dataset.presented = 'false';
        next.nodes.git.dataset.validationError = 'saved preview invalid';
        next.nodes.git.dataset.snapshotId = '';
      } else if (testCase === 'snapshot-content-changed') {
        next.nodes.git.dataset.snapshotId = 'old';
      }
      const preview = document.nodes.git.querySelector('[data-preview]');
      const text = preview.querySelector('pre'), html = text.innerHTML;
      if (testCase === 'snapshot-focused') document.activeElement = preview.querySelector('summary');
      else selection = {isCollapsed:false, anchorNode:preview.querySelector('pre').childNodes[0],
        focusNode:preview.querySelector('pre').childNodes[0]};
      const selected = selection;
      await run('refresh()');
      oldPreview(); assert.equal(preview.querySelector('pre'), text);
      assert.equal(text.innerHTML, html); assert.equal(selection, selected);
      invalidConfirmation();
      assertDisplayDecision('Git 影響已變更，請重新展示', /舊確認已停用.*重新展示/);
      const before = posts().length;
      await click(confirmButton());
      await run("post('/api/deliver', {confirm:true,snapshot_id:'old'})");
      assert.equal(posts().length, before);
      request = async (url, options) => options.method === 'POST' ?
        response('new', {delivery_preview_presented:true, delivery_snapshot_id:'new'}) : response();
      next.nodes.git.dataset.presented = 'true'; next.nodes.git.dataset.snapshotId = 'new';
      next.nodes.git.dataset.validationError = '';
      await click(displayButton());
      assert.equal(posts().length, before + 1); oldPreview();
      assert.ok(confirmButton().disabled);
      assert.match(feedback('delivery-feedback'), /新展示尚未套用/);
      assertDisplayDecision('新 Git 展示尚待套用', /保留閱讀內容.*套用新展示後才能另行確認/);
      assert.equal(selection, selected);
      selection = {isCollapsed:true}; document.activeElement = document.body;
      await run('refresh()'); oldPreview();
      closeReading(preview); dispatch('toggle');
      assert.equal(run('deliverySnapshotId'), 'new');
      assert.ok(preview.textContent.includes('impact-new'));
      assert.equal(confirmButton().dataset.snapshotId, 'new');
      assertConfirmation();
      assert.equal(confirmButton().disabled, false);
      assert.equal(confirmButton().className, 'primary');
      assert.equal(document.nodes['decision-copy'].querySelector('h3').textContent,
        '已展示 Git 影響，等待二次確認');
      assert.equal(document.nodes['decision-copy'].textContent, next.nodes['decision-copy'].textContent);
      assert.equal(feedback('decision-status'), '');
      assert.deepEqual(buttons().filter(node => !node.disabled && node.className === 'primary')
        .map(node => node.dataset.action), ['confirm-delivery']);
      assert.match(feedback('delivery-feedback'), /另行確認/);
      assert.equal(posts().length, before + 1);
      await run("post('/api/deliver', {confirm:true,snapshot_id:'old'})");
      assert.equal(posts().length, before + 1); invalidConfirmation();
      await click(displayButton());
      const displayedCount = posts().length;
      assert.equal(JSON.parse(posts().at(-1).options.body).confirm, undefined);
      await click(confirmButton());
      assert.equal(posts().length, displayedCount + 1);
      const body = JSON.parse(posts().at(-1).options.body);
      assert.equal(body.confirm, true); assert.equal(body.snapshot_id, document.nodes.git.dataset.snapshotId);
      assert.equal(body.snapshot_id, 'new');
      break;
    }
    case 'matrix': {
      for (const region of ['actions','delivery-actions','reset-actions']) {
        const node = document.nodes[region];
        document.activeElement = node.children[0] || document.input;
        const focused = document.activeElement;
        await run('refresh()');
        assert.equal(document.activeElement, focused); assert.ok(document.contains(focused));
      }
      assert.equal(document.nodes['decision-copy'].querySelector('h3').textContent, payload.expected.title);
      const actual = buttons().filter(node => !node.disabled).map(node => node.dataset.action).sort();
      assert.deepEqual(actual, payload.expected.enabled.slice().sort());
      for (const node of buttons()) {
        const enabled = payload.expected.enabled.includes(node.dataset.action);
        assert.equal(node.disabled, !enabled, node.dataset.action);
      }
      const primary = buttons().filter(node => !node.disabled && node.className === 'primary');
      assert.deepEqual(primary.map(node => node.dataset.action),
        payload.expected.primary ? [payload.expected.primary] : []);
      const count = posts().length;
      for (const expression of [
        "post('/api/continue')", "post('/api/task', {prompt:'blocked'})",
        "post('/api/approve')", "post('/api/review', {decision:'revise'})",
        "post('/api/deliver', {confirm:true,snapshot_id:deliverySnapshotId})",
      ]) {
        const key = expression.includes('/continue') ? 'continue{}' :
          expression.includes('/task') ? 'task{}' :
          expression.includes('/approve') ? 'approve{}' :
          expression.includes('/review') ? 'review{"decision":"revise"}' : 'confirm-delivery';
        if (!payload.expected.enabled.includes(key)) await run(expression);
      }
      assert.equal(posts().length, count);
      assertApplied(3);
      break;
    }
    case 'post-decision': {
      const posted = defer(), reread = defer(), started = defer();
      request = (url, options) => {
        if (options.method === 'POST') return posted.promise;
        started.resolve(); return reread.promise;
      };
      const expression = payload.endpoint === 'approve' ? "post('/api/approve')" :
        "post('/api/review', {decision:'revise'})";
      const action = run(expression);
      await run(expression); await run('refresh()');
      assert.equal(posts().length, 1); assert.equal(requests.length, 1);
      allDisabled(); assert.match(feedback('action-feedback'), /處理中/);
      assert.equal(timestampApplications.length, 0);
      posted.resolve(response());
      await started.promise;
      allDisabled();
      assert.match(feedback('action-feedback'), /操作已受理/);
      assert.equal(timestampApplications.length, 0); assert.equal(feedback('last-refresh'), '');
      await run(expression); await submit();
      assert.equal(posts().length, 1);
      reread.resolve(response()); await action;
      currentPhase('new'); currentTasks('new'); assertApplied(1);
      assert.equal(run('csrf'), 'csrf-new');
      for (const node of buttons()) {
        if (node.dataset.action !== 'stop{}') assert.equal(node.disabled, true);
      }
      assert.equal(posts()[0].options.headers['X-CSRF-Token'], 'csrf-old');
      if (payload.endpoint === 'revise')
        assert.equal(JSON.parse(posts()[0].options.body).decision, 'revise');
      else assert.equal(posts()[0].url, '/api/approve');
      break;
    }
    case 'delivery-post-errors': {
      const held = defer();
      request = (url, options) => options.method === 'POST' ? held.promise : Promise.resolve(response());
      const expression = payload.endpoint === 'confirm' ?
        "post('/api/deliver', {confirm:true,snapshot_id:deliverySnapshotId})" : "post('/api/deliver')";
      const action = run(expression);
      await run(expression);
      assert.equal(posts().length, 1); allDisabled();
      assert.match(feedback('action-feedback'), payload.endpoint === 'confirm' ? /正在驗證/ : /正在準備/);
      if (payload.failure === 'network') held.resolve(Promise.reject(Error('offline')));
      else held.resolve({ok:payload.failure !== 'http', json:async () => {
        if (payload.failure === 'json') throw Error('invalid JSON');
        return {error:'saved preview invalid'};
      }});
      await action;
      assertApplied(1); oldPreview(); invalidConfirmation();
      assert.match(feedback('action-feedback'), payload.failure === 'http' ? /操作未完成/ : /操作結果尚未確認/);
      if (payload.endpoint === 'confirm') {
        await run(expression); assert.equal(posts().length, 1);
      }
      request = async (url, options) => options.method === 'POST' ?
        response('new', {delivery_preview_presented:true, delivery_snapshot_id:'old'}) : response();
      await click(displayButton());
      assert.equal(confirmButton().disabled, false);
      assert.match(feedback('delivery-feedback'), /另行確認/);
      const count = posts().length;
      await click(confirmButton());
      assert.equal(posts().length, count + 1);
      assert.equal(JSON.parse(posts().at(-1).options.body).snapshot_id, 'old');
      break;
    }
    case 'post-errors': {
      for (const kind of ['http','network','json']) {
        const held = defer();
        request = (url, options) => options.method === 'POST' ? held.promise : Promise.resolve(response());
        document.input.value = 'keep draft';
        const action = submit();
        await submit(); await run("post('/api/task', {prompt:'keep draft'})");
        const before = posts().length;
        allDisabled(); assert.match(feedback('action-feedback'), /處理中/);
        if (kind === 'network') held.resolve(Promise.reject(Error('offline')));
        else held.resolve({ok:kind !== 'http', json:async () => {
          if (kind === 'json') throw Error('invalid JSON');
          return {error:'Rejected'};
        }});
        await action;
        assert.equal(posts().length, before);
        assert.equal(document.input.value, 'keep draft');
        assert.equal(document.submit.disabled, false);
        assert.match(feedback('action-feedback'), kind === 'http' ? /操作未完成：Rejected/ : /操作結果尚未確認/);
        assert.equal(feedback('refresh-error'), '');
      }
      assert.equal(posts().length, 3);
      assert.deepEqual(alerts, ['Rejected']); assertApplied(3);
      break;
    }
    case 'post-refresh-failure': {
      damage(invalid, payload.failure);
      closeReading(document.nodes.git);
      const held = defer();
      request = (url, options) => options.method === 'POST' ? held.promise : failResponse(payload.failure)();
      document.input.value = 'submitted';
      const action = payload.endpoint === 'task' ? submit() :
        run(payload.endpoint === 'confirm' ?
          "post('/api/deliver', {confirm:true,snapshot_id:deliverySnapshotId})" :
          "post('/api/deliver')");
      allDisabled();
      await run("post('/api/continue')"); await submit();
      assert.equal(posts().length, 1);
      held.resolve(response('new', {delivery_preview_presented:true, delivery_snapshot_id:'new'}));
      await action;
      allDisabled();
      assert.equal(feedback('last-refresh'), '');
      assert.equal(timestampApplications.length, 0);
      assert.equal(feedback('connection-status'), '連線異常，操作暫停');
      assert.match(feedback('refresh-error'), /更新失敗/);
      const count = posts().length;
      await submit(); await run("post('/api/continue')");
      await run("post('/api/deliver', {confirm:true,snapshot_id:deliverySnapshotId})");
      assert.equal(posts().length, count);
      request = async () => response();
      clock += 60000; await run('refresh()'); assertApplied(1);
      if (payload.endpoint === 'task') assert.equal(document.input.value, '');
      else if (payload.endpoint === 'display') {
        assert.equal(confirmButton().disabled, false);
        assert.equal(run('deliverySnapshotId'), 'new');
        assert.match(feedback('delivery-feedback'), /另行確認/);
        assert.equal(posts().length, count);
      } else invalidConfirmation();
      break;
    }
    case 'repeated-task-snapshots': {
      const tasks = document.nodes.tasks, original = tasks.children.slice();
      const first = original[0].querySelector('details'), last = original.at(-1).querySelector('details');
      const pre = first.querySelector('pre'), text = pre.innerHTML;
      pre.scrollTop = 81; pre.scrollLeft = 9; tasks.scrollTop = 92;
      document.input.value = '保留需求草稿';
      const protection = payload.expected.protection;
      if (protection === 'expanded') { first.open = true; last.open = true; }
      if (protection === 'focus') document.activeElement = first.querySelector('summary');
      if (protection === 'selection')
        selection = {isCollapsed:false, anchorNode:pre.childNodes[0],
          focusNode:last.querySelector('pre').childNodes[0]};
      const selected = selection, focused = document.activeElement;
      const preserved = () => {
        assert.deepEqual(tasks.children, original);
        assert.equal(first.querySelector('pre'), pre); assert.equal(pre.innerHTML, text);
        assert.equal(pre.scrollTop, 81); assert.equal(pre.scrollLeft, 9);
        assert.equal(tasks.scrollTop, 92); assert.equal(document.input.value, '保留需求草稿');
        assert.equal(selection, selected); assert.equal(document.activeElement, focused);
        assert.equal(window.scrollX, 20); assert.equal(window.scrollY, 400);
      };
      request = failResponse('network'); await run('refresh()');
      allDisabled(); preserved(); assert.equal(feedback('last-refresh'), '');
      let count = 0;
      for (const [index, version] of payload.expected.versions.entries()) {
        const incoming = snapshots[version].nodes.tasks;
        const keys = incoming.children.map(node => node.dataset.task);
        assert.equal(new Set(keys).size, keys.length);
        assert.ok(keys.every(Boolean));
        for (const node of incoming.children)
          assert.equal(node.querySelector('details').dataset.detail, node.dataset.task);
        request = async () => response(version);
        clock += 60000; await run('refresh()'); assertApplied(++count);
        currentPhase(version); preserved(); assertNotice('tasks-reading-status', tasks);
        const stop = document.querySelector('[data-action="stop{}"]');
        if (payload.expected.stop[index]) { assert.ok(stop); assert.equal(stop.disabled, false); }
        else assert.ok(!stop || stop.disabled);
        const stamp = feedback('last-refresh');
        clock += 60000; await run('refresh()'); assertApplied(++count);
        assert.notEqual(feedback('last-refresh'), stamp); preserved();
      }
      const version = payload.expected.versions.at(-1), incoming = snapshots[version].nodes.tasks;
      const requestCount = requests.length;
      if (protection === 'expanded') {
        first.open = false; dispatch('toggle'); preserved();
        assertNotice('tasks-reading-status', tasks);
        last.open = false; dispatch('toggle');
      } else if (protection === 'focus') {
        document.activeElement = document.body;
        dispatch('focusout');
        const timer = timers.findIndex(item => item.delay === 0);
        assert.ok(timer >= 0); timers.splice(timer, 1)[0].callback();
      } else {
        await click(document.querySelector('[data-action="stop{}"]'));
        assert.equal(posts().length, 1); assert.equal(posts()[0].url, '/api/stop');
        preserved();
        selection = {isCollapsed:true}; dispatch('selectionchange');
      }
      assert.equal(requests.length, requestCount + (protection === 'selection' ? 2 : 0));
      assert.deepEqual(tasks.children.map(node => node.dataset.task),
        incoming.children.map(node => node.dataset.task));
      assert.equal(tasks.textContent, incoming.textContent);
      assert.ok(tasks.querySelectorAll('details').every(node => !node.open));
      assert.equal(feedback('tasks-reading-status'), '');
      assert.equal(document.input.value, '保留需求草稿');
      break;
    }
    case 'expanded-reading-sequence': {
      const id = payload.expected.region, region = document.nodes[id];
      const reading = id === 'git' ? region.querySelector('[data-preview]') : region;
      const detail = id === 'reports' ? document.panel :
        id === 'git' ? reading.querySelector('details') : reading;
      const pre = detail.querySelector('pre'), html = pre.innerHTML;
      const notice = payload.expected.notice;
      detail.open = true; pre.scrollTop = 65; pre.scrollLeft = 4;
      document.activeElement = document.input;
      const original = reading.childNodes.slice();
      let count = 0;
      for (const version of ['new', 'removed', 'restored']) {
        request = async () => response(version);
        for (let poll = 0; poll < 2; poll++) {
          clock += 60000; await run('refresh()'); assertApplied(++count);
          currentPhase(version);
          assert.deepEqual(reading.childNodes, original);
          assert.equal(detail.open, true); assert.equal(detail.querySelector('pre'), pre);
          assert.equal(pre.innerHTML, html); assert.equal(pre.scrollTop, 65);
          assert.equal(pre.scrollLeft, 4); assert.equal(document.activeElement, document.input);
          assertNotice(notice, reading);
          assert.equal(document.querySelector('[data-action="stop{}"]').disabled, false);
        }
      }
      const countBefore = requests.length;
      detail.open = false; dispatch('toggle');
      assert.equal(requests.length, countBefore);
      assert.ok(reading.textContent.includes(payload.expected.latest));
      assert.equal(feedback(notice), '');
      assert.equal(window.scrollX, 20); assert.equal(window.scrollY, 400);
      break;
    }
    case 'decision-selection-sequence': {
      const copy = document.nodes['decision-copy'], original = copy.childNodes.slice();
      const html = copy.innerHTML;
      selection = {isCollapsed:false, anchorNode:copy.querySelector('h3').childNodes[0],
        focusNode:copy.querySelector('p').childNodes[0]};
      const selected = selection, status = document.nodes['decision-status'];
      assert.equal(status.attrs.role, 'status'); assert.equal(status.hidden, false);
      assert.equal(copy.contains(status), false);
      let count = 0;
      for (const [index, version] of payload.expected.versions.entries()) {
        request = async () => response(version); clock += 60000;
        await run('refresh()'); assertApplied(++count); currentPhase(version);
        assert.equal(selection, selected); assert.equal(copy.innerHTML, html);
        assert.deepEqual(copy.childNodes, original);
        const incoming = snapshots[version].nodes['decision-copy'];
        const latest = incoming.children.map(node => node.textContent.trim()).join('。');
        assert.equal(status.textContent, latest);
        assert.ok(latest.includes(payload.expected.titles[index]));
        assert.ok(latest.includes(payload.expected.effects[index]));
        assert.ok(status.childNodes.every(node => node instanceof Text));
        const enabled = buttons().filter(node => !node.disabled).map(node => node.dataset.action).sort();
        assert.deepEqual(enabled, payload.expected.enabled[index].slice().sort());
        const primary = buttons().filter(node => !node.disabled && node.className === 'primary');
        assert.deepEqual(primary.map(node => node.dataset.action),
          payload.expected.primary[index] ? [payload.expected.primary[index]] : []);
        const announcements = textMutations.filter(item => item.node === status).length;
        clock += 60000; await run('refresh()'); assertApplied(++count);
        assert.equal(textMutations.filter(item => item.node === status).length, announcements);
        assert.equal(selection, selected); assert.equal(copy.innerHTML, html);
      }
      assert.equal(posts().length, 0);
      selection = {isCollapsed:true}; await run('refresh()'); assertApplied(++count);
      assert.equal(copy.textContent, snapshots[payload.expected.versions.at(-1)].nodes['decision-copy'].textContent);
      assert.equal(status.textContent, '');
      break;
    }
    default: throw Error('Unknown test case: ' + testCase);
  }
  assert.ok(timers.length > 0);
  assert.ok(timers.every(timer => timer.delay === 3000));
  assert.ok(requests.every(item => item.options.credentials === 'same-origin'));
  assert.ok(requests.filter(item => !item.options.method).every(item =>
    item.url === '/' && item.options.cache === 'no-store'));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


def rendered_fixture(version, **fields):
    """Build actual server controls; expected decisions stay in the test matrix."""
    status = {
        "project_name": f"phase-{version}",
        "csrf": f"csrf-{version}",
        "state": "WAITING_FOR_DELIVERY",
        "delivery_artifacts_prepared": True,
        "delivery_preview_presented": True,
        "delivery_snapshot_id": version,
        "git": {"diff_stat": f"impact-{version}"},
        "reports": {
            name: f"{name}-{version}\n完整報告"
            for name in ("qa", "tests", "review", "plan", "final")
        },
        "progress": {
            "tasks": [
                {"id": key, "description": f"description-{version}-{key}\n完整描述"}
                for key in ("alpha", "beta")
            ]
        },
        **fields,
    }
    return RenderedDOM(render_page(status)).root


def run_harness(
    case, *, fields=None, expected=None, endpoint=None, failure=None, snapshots=None
):
    node = shutil.which("node")
    if not node:
        pytest.skip(
            "Node.js is needed to execute the GUI script; no npm packages required"
        )
    fields = dict(fields or {})
    if case in {"failure", "submit", "post-errors", "status-deferred", "polling"} or (
        case == "post-refresh-failure" and endpoint == "task"
    ):
        fields = {
            "state": "PLANNING",
            "can_submit": True,
            "delivery_preview_presented": False,
            **fields,
        }
    if case in {"delivery", "selection"}:
        fields = {"delivery_preview_presented": False, **fields}
    pages = {
        version: rendered_fixture(version, **fields)
        for version in ("old", "new", "stale", "invalid")
    }
    if case in {"delivery", "selection"}:
        pages["new"] = rendered_fixture("new")
    if case == "post-decision":
        pages["new"] = rendered_fixture(
            "new",
            state="IMPLEMENTING" if endpoint == "approve" else "DEVELOPING_PLAN",
            worker_running=True,
            delivery_preview_presented=False,
        )
    if case == "delivery-post-errors":
        for version in pages:
            pages[version] = rendered_fixture(
                version, delivery_snapshot_id="old", git={"diff_stat": "impact-old"}
            )
    if case == "race":
        pages["old"] = rendered_fixture(
            "old", state="PLANNING", can_submit=False, delivery_preview_presented=False
        )
        pages["new"] = rendered_fixture(
            "new", state="PLANNING", can_submit=True, delivery_preview_presented=False
        )
    if case == "decision-preserved":
        pages["old"] = rendered_fixture(
            "old", state="PLANNING", can_submit=False, delivery_preview_presented=False
        )
        pages["new"] = rendered_fixture(
            "new", state="IMPLEMENTING", worker_running=True, pause_requested=True
        )
    if case == "matrix":
        # Stable preview identity lets this matrix isolate control availability.
        for version in pages:
            pages[version] = rendered_fixture(
                version,
                **{
                    **fields,
                    "delivery_snapshot_id": "snapshot",
                    "git": {"diff_stat": "stable"},
                },
            )
        pages["old"] = rendered_fixture(
            "old",
            state="PLANNING",
            can_submit=False,
            delivery_preview_presented=False,
            delivery_snapshot_id="snapshot",
            git={"diff_stat": "stable"},
        )
    if snapshots:
        pages.update(
            {
                version: rendered_fixture(version, **status)
                for version, status in snapshots.items()
            }
        )
    page = render_page({"csrf": "csrf-old", "delivery_snapshot_id": "old"})
    script = re.search(r"<script>(.*?)</script>", page, re.DOTALL)
    assert script is not None, "The rendered GUI must contain its production script"
    result = subprocess.run(
        [node, "-e", HARNESS, case],
        input=json.dumps(
            {
                "script": script.group(1),
                "pages": pages,
                "expected": expected,
                "endpoint": endpoint,
                "failure": failure,
            }
        ),
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, f"{case}\n{result.stdout}\n{result.stderr}"


FAILURES = [
    "network",
    "http",
    "body",
    "parse",
    "missing-region",
    "missing-csrf",
    "empty-csrf",
    "missing-state",
    "missing-input",
    "invalid-report",
    "missing-summary",
    "duplicate-report",
    "missing-report-key",
    "duplicate-task",
    "missing-task-key",
    "missing-preview",
    "invalid-enabled",
    "duplicate-control",
    *[
        f"region:{region}"
        for region in (
            "workflow-status",
            "tasks",
            "task-form",
            "git",
            "reports",
            "reset-actions",
            "delivery-actions",
            "display-actions",
            "decision-copy",
            "current-review",
        )
    ],
]


@pytest.mark.parametrize(
    "case",
    [
        "initial",
        *[f"flow-{kind}" for kind in FAILURES],
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
        "polling",
        "protected-membership",
        "task-membership",
        "range-selection",
        "decision-preserved",
        "snapshot-changed",
        "snapshot-invalid",
        "snapshot-content-changed",
        "snapshot-focused",
        "post-errors",
    ],
)
def test_gui_refresh_behavior(case):
    """Run the production JavaScript offline against a simulated DOM."""
    run_harness(case)


# Reuse the owner's decision evidence and labels already covered by task-2.
@pytest.mark.parametrize(
    "source,reason,resume_state,pass_state,title,label,action", OWNER_REVIEW_CASES
)
def test_gui_refresh_owner_decision_matrix(
    source, reason, resume_state, pass_state, title, label, action
):
    fields = owner_status(source, reason, resume_state, pass_state)
    fields["delivery_preview_presented"] = False
    enabled = {
        ("核准實作", "approve{}"): [
            "approve{}",
            'review{"decision":"revise"}',
            'review{"decision":"reject"}',
            "reset{}",
        ],
        ("提交修訂", 'review{"decision":"revise"}'): [
            'review{"decision":"revise"}',
            'review{"decision":"pass"}',
            'review{"decision":"reject"}',
            "reset{}",
        ],
        ("重試 QA 驗證", "approve{}"): [
            "approve{}",
            'review{"decision":"reject"}',
            "reset{}",
        ],
        ("重試 QA 驗證", "continue{}"): [
            "continue{}",
            'review{"decision":"reject"}',
            "reset{}",
        ],
        (None, None): [
            'review{"decision":"revise"}',
            'review{"decision":"pass"}',
            "continue{}",
            'review{"decision":"reject"}',
            "reset{}",
        ],
    }[(label, action)]
    run_harness(
        "matrix",
        fields=fields,
        expected={
            "title": title,
            "enabled": enabled,
            "primary": action,
        },
    )


@pytest.mark.parametrize(
    "fields,title,primary,enabled",
    [
        (
            {"state": "PLANNING", "can_submit": True},
            "提交新任務",
            "task{}",
            ["task{}", "reset{}"],
        ),
        (
            {"state": "PLANNING", "can_submit": False},
            "等待下一步",
            "continue{}",
            ["continue{}", "reset{}"],
        ),
        (
            {"state": "IMPLEMENTING", "worker_running": True},
            "執行中",
            "stop{}",
            ["stop{}"],
        ),
        (
            {"state": "IMPLEMENTING", "worker_running": True, "pause_requested": True},
            "目前步驟完成後暫停",
            None,
            [],
        ),
        (
            owner_status(
                "Orchestrator",
                "Paused at step boundary.",
                "IMPLEMENTING",
                "IMPLEMENTING",
            ),
            "已暫停，等待擁有者決策",
            "continue{}",
            [
                "continue{}",
                'continue{"clear_route_failures":true}',
                'review{"decision":"reject"}',
                "reset{}",
            ],
        ),
        ({"state": "WAITING_FOR_OWNER", "worker_running": True}, "執行中", None, []),
        (
            {"state": "WAITING_FOR_DELIVERY", "delivery_artifacts_prepared": False},
            "準備交付資料中",
            "deliver{}",
            ["deliver{}", "reset{}"],
        ),
        (
            {
                "state": "WAITING_FOR_DELIVERY",
                "delivery_artifacts_prepared": False,
                "worker_running": True,
            },
            "正在準備交付資料",
            None,
            [],
        ),
        (
            {"state": "WAITING_FOR_DELIVERY", "delivery_artifacts_prepared": True},
            "等待展示 Git 影響",
            "deliver{}",
            ["deliver{}", "reset{}"],
        ),
        (
            {
                "state": "WAITING_FOR_DELIVERY",
                "delivery_artifacts_prepared": True,
                "delivery_preview_presented": True,
            },
            "已展示 Git 影響，等待二次確認",
            "confirm-delivery",
            ["deliver{}", "confirm-delivery", "reset{}"],
        ),
        (
            {
                "state": "WAITING_FOR_DELIVERY",
                "delivery_artifacts_prepared": False,
                "delivery_preview_presented": True,
            },
            "準備交付資料中",
            "deliver{}",
            ["deliver{}", "reset{}"],
        ),
        (
            {
                "state": "WAITING_FOR_DELIVERY",
                "delivery_artifacts_prepared": True,
                "worker_status": {"phase": "failed", "error": "Git impact changed"},
            },
            "等待展示 Git 影響",
            "deliver{}",
            ["deliver{}", "reset{}"],
        ),
        (
            {
                "state": "WAITING_FOR_DELIVERY",
                "delivery_artifacts_prepared": True,
                "delivery_preview_presented": True,
                "worker_running": True,
            },
            "正在驗證並處理交付",
            None,
            [],
        ),
        (
            {"state": "COMPLETED", "delivery": {"delivered": True}, "can_submit": True},
            "已完成交付",
            "task{}",
            ["task{}", "reset{}"],
        ),
        (
            {
                "state": "COMPLETED",
                "delivery": {"delivered": False},
                "can_submit": False,
            },
            "流程已完成，交付結果待檢視",
            None,
            ["reset{}"],
        ),
        (
            {"state": "FAILED", "can_submit": True},
            "流程失敗",
            "task{}",
            ["task{}", "reset{}"],
        ),
    ],
)
def test_gui_refresh_lifecycle_decision_matrix(fields, title, primary, enabled):
    run_harness(
        "matrix",
        fields={"delivery_preview_presented": False, **fields},
        expected={"title": title, "primary": primary, "enabled": enabled},
    )


@pytest.mark.parametrize("endpoint", ["approve", "revise"])
def test_gui_post_owner_decision_refreshes_before_enabling_actions(endpoint):
    fields = owner_status(
        "Architect",
        (
            "Architect approved the plan. The original request requires "
            "owner approval before implementation."
            if endpoint == "approve"
            else "PLAN_STATUS: REJECTED\nRevise app.py."
        ),
        "IMPLEMENTING" if endpoint == "approve" else "DEVELOPING_PLAN",
        "IMPLEMENTING",
        delivery_preview_presented=False,
    )
    run_harness("post-decision", fields=fields, endpoint=endpoint)


@pytest.mark.parametrize("endpoint", ["display", "confirm"])
@pytest.mark.parametrize("failure", ["http", "network", "json"])
def test_gui_delivery_post_failure_requires_another_display(endpoint, failure):
    run_harness("delivery-post-errors", endpoint=endpoint, failure=failure)


@pytest.mark.parametrize("failure", ["http", "network", "body", "parse"])
def test_gui_post_wins_over_failed_inflight_poll(failure):
    run_harness("race", failure=failure)


@pytest.mark.parametrize("endpoint", ["task", "display", "confirm"])
@pytest.mark.parametrize(
    "failure", ["network", "http", "body", "parse", "missing-csrf"]
)
def test_gui_post_requires_successful_status_reread(endpoint, failure):
    run_harness("post-refresh-failure", endpoint=endpoint, failure=failure)


@pytest.mark.parametrize(
    "node_id",
    [
        "last-refresh",
        "refresh-error",
        "action-feedback",
        "delivery-feedback",
        "decision-status",
        "tasks-reading-status",
        "reports-reading-status",
        "review-reading-status",
        "git-reading-status",
    ],
)
def test_gui_refresh_initial_markup_has_empty_accessible_status(node_id):
    """Inspect rendered HTML without treating the DOM double as an HTML parser."""
    page = render_page({"csrf": "csrf-old", "delivery_snapshot_id": "old"})
    assert re.search(
        rf'<p\b(?=[^>]*\bid="{node_id}")(?=[^>]*\brole="status")[^>]*>\s*</p>',
        page,
    )


def task_nodes(status):
    document = RenderedDOM(render_page(status)).root

    def descendants(node):
        if isinstance(node, dict):
            yield node
            for child in node["children"]:
                yield from descendants(child)

    return [
        node
        for node in descendants(document)
        if node["tag"] == "li" and "data-task" in node["attrs"]
    ]


def test_gui_task_identity_is_canonical_unique_and_does_not_mutate_tasks():
    task = {
        "id": "HUMAN-REVIEW-1",
        "description": "修訂需求\n完整描述 <script>不執行</script>",
        "target_files": ["z.py", "a.py", "z.py"],
        "status": "PENDING",
        "assigned_to": "RD-1",
    }
    tasks = [
        task,
        copy.deepcopy(task),
        {**task, "description": "另一份修訂需求"},
        {**task, "id": ""},
        {**task, "id": ""},
    ]
    status = {"progress": {"tasks": tasks, "code_revisions": 3}}
    original = copy.deepcopy(status)
    nodes = task_nodes(status)
    assert status == original
    occurrences = {}
    expected = []
    for item in tasks:
        canonical = json.dumps(
            {
                "id": item["id"],
                "description": item["description"],
                "target_files": sorted(set(item["target_files"])),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        occurrences[fingerprint] = occurrences.get(fingerprint, 0) + 1
        expected.append(f"task-{fingerprint}-{occurrences[fingerprint]}")
    keys = [node["attrs"]["data-task"] for node in nodes]
    assert keys == expected
    assert len(set(keys)) == len(tasks)
    assert all(
        next(child for child in node["children"] if child["tag"] == "details")["attrs"][
            "data-detail"
        ]
        == key
        for node, key in zip(nodes, keys)
    )
    revised = copy.deepcopy(status)
    revised.update(worker_running=True, personnel={"RD": "RD-2"})
    revised["progress"].update(
        active_task_id="HUMAN-REVIEW-1", code_revisions=0, plan_revisions=0
    )
    for item in revised["progress"]["tasks"]:
        item.update(status="RUNNING", assigned_to="RD-2")
        item["target_files"] = ["a.py", "z.py"]
    revised_original = copy.deepcopy(revised)
    assert [node["attrs"]["data-task"] for node in task_nodes(revised)] == keys
    assert revised == revised_original
    for field, value in [
        ("id", "HUMAN-REVIEW-2"),
        ("description", "更新描述"),
        ("target_files", ["a.py", "changed.py"]),
    ]:
        changed = {**task, field: value}
        assert (
            task_nodes({"progress": {"tasks": [changed]}})[0]["attrs"]["data-task"]
            != keys[0]
        )


@pytest.mark.parametrize(
    "identity", ["same-description", "different-description", "empty-id"]
)
@pytest.mark.parametrize("protection", ["expanded", "focus", "selection"])
def test_gui_repeated_human_review_tasks_preserve_reading_and_apply_latest(
    identity, protection
):
    task = {
        "id": "" if identity == "empty-id" else "HUMAN-REVIEW-1",
        "description": "原修訂需求",
        "target_files": ["a.py"],
        "status": "PENDING",
    }
    second = {
        **task,
        "description": "新修訂需求"
        if identity == "different-description"
        else task["description"],
    }
    sibling = {"id": "RD-1", "description": "既有任務", "target_files": ["b.py"]}
    original = [task, sibling]
    versions = {
        "new": [task, sibling, second],
        "removed": [second],
        "changed": [
            {**second, "description": "描述已更新", "target_files": ["c.py"]},
            sibling,
            task,
        ],
        "targets": [
            {**second, "description": "描述已更新", "target_files": ["d.py"]},
            sibling,
            task,
        ],
        "reordered": [second, task, sibling],
        "restored": [task, sibling, second, copy.deepcopy(second)],
    }

    def snapshot(items, revisions, paused=False):
        return {
            "state": "IMPLEMENTING",
            "worker_running": True,
            "pause_requested": paused,
            "delivery_preview_presented": False,
            "progress": {
                "tasks": copy.deepcopy(items),
                "active_task_id": task["id"],
                "code_revisions": revisions,
            },
        }

    snapshots = {"old": snapshot(original, 3)}
    snapshots.update(
        {
            version: snapshot(items, 0, paused=version == "changed")
            for version, items in versions.items()
        }
    )
    run_harness(
        "repeated-task-snapshots",
        snapshots=snapshots,
        expected={
            "protection": protection,
            "versions": list(versions),
            "stop": [True, True, False, True, True, True],
        },
    )


@pytest.mark.parametrize(
    "region,notice,latest",
    [
        ("reports", "reports-reading-status", "qa-restored"),
        ("current-review", "review-reading-status", "審核原因 restored"),
        ("git", "git-reading-status", "impact-restored"),
    ],
)
def test_gui_expanded_unfocused_reading_survives_update_removal_and_restoration(
    region, notice, latest
):
    snapshots = {}
    for version in ("old", "new", "removed", "restored"):
        status = {
            "state": "IMPLEMENTING",
            "worker_running": True,
            "delivery_preview_presented": False,
            "review": {"source": "QA", "reason": f"審核原因 {version}"},
        }
        if version == "removed":
            status.update(reports={}, review={}, git={})
        snapshots[version] = status
    run_harness(
        "expanded-reading-sequence",
        snapshots=snapshots,
        expected={"region": region, "notice": notice, "latest": latest},
    )


def test_gui_selected_running_decision_tracks_owner_decisions_and_returns_to_running():
    cases = {case.id: case.values for case in OWNER_REVIEW_CASES}
    snapshots = {
        "old": {
            "state": "IMPLEMENTING",
            "worker_running": True,
            "delivery_preview_presented": False,
        }
    }
    titles = []
    for version, case_id in (
        ("approved", "architect-approved-with-embedded-rejection"),
        ("rejected", "architect-rejected"),
        ("neutral", "insufficient-evidence"),
    ):
        source, reason, resume, passed, title, _, _ = cases[case_id]
        snapshots[version] = owner_status(
            source, reason, resume, passed, delivery_preview_presented=False
        )
        titles.append(title)
    snapshots["running"] = {
        "state": "TESTING",
        "worker_running": True,
        "stage": "<img src=x onerror=alert(1)>",
        "delivery_preview_presented": False,
    }
    run_harness(
        "decision-selection-sequence",
        snapshots=snapshots,
        expected={
            "versions": ["approved", "rejected", "neutral", "running"],
            "titles": [*titles, "執行中"],
            "effects": [
                "核准後將進入實作",
                "重新撰寫計畫",
                "次要操作的效果不同",
                "背景工作結束後會更新狀態",
            ],
            "enabled": [
                [
                    "approve{}",
                    'review{"decision":"revise"}',
                    'review{"decision":"reject"}',
                    "reset{}",
                ],
                [
                    'review{"decision":"revise"}',
                    'review{"decision":"pass"}',
                    'review{"decision":"reject"}',
                    "reset{}",
                ],
                [
                    'review{"decision":"revise"}',
                    'review{"decision":"pass"}',
                    "continue{}",
                    'review{"decision":"reject"}',
                    "reset{}",
                ],
                ["stop{}"],
            ],
            "primary": ["approve{}", 'review{"decision":"revise"}', None, "stop{}"],
        },
    )


# plan_notes validation:
# Attempted: python3 -m pytest -q -rs test_gui.py test_gui_refresh.py
# Attempted: python3 -m pytest -q -rs
# Both commands stopped before collection: this read-only environment has no
# writable temporary directory for pytest's default file-descriptor capture.
# The candidate was collected in memory with --capture=sys -p no:cacheprovider:
# 108 cases in this file and 28 existing decision-matrix cases passed, no skips.
# Node v24.13.0 was available. Node-dependent cases must report skips with -rs
# when Node is absent; a skip does not provide JavaScript execution evidence.
# Skipped: full regression and real-browser acceptance remain outstanding.
# Risk: the DOM double cannot establish native HTML, focus, Range, keyboard or layout behavior.
#
# plan_notes.verification (FIX-REV-3-2; the preceding block is historical):
# Read-only baseline with sys capture: 44 failed, 64 passed.
# Final candidate compiled and collected in memory; no workspace files were written.
# Candidate: 127 passed with --capture=sys -p no:cacheprovider, no skips.
# Combined with existing offline owner/lifecycle matrices: 155 passed,
# 117 deselected, no skips. Node v24.13.0 executed the production JavaScript.
# Covered: duplicate-task rejection, repeated HUMAN-REVIEW-1 and empty IDs,
# canonical identity, revision reset, task membership/content changes,
# deferred reading, removal/restoration, event-driven application, selection,
# decision explanations, announcement deduplication, recovery and POST races.
# Attempted: python3 -m pytest -q -rs -p no:cacheprovider test_gui.py test_gui_refresh.py
# Attempted: python3 -m pytest -q -rs -p no:cacheprovider
# Both stopped before collection: FD capture had no writable temporary directory.
# Retrying test_gui.py with sys capture and -x stopped at its first tmp_path fixture.
# Independent senior QA reviewed the candidate design and ran the existing
# decision matrices: 28 passed, 117 deselected, no skips.
# QA did not independently execute the in-memory candidate.
# Parent test_gui.py work remains outside this target: restore the display-response
# expectation and use Event/Barrier coverage for display-lock release,
# preparation, confirmation and external lock holders.
# Confirmation ancestry/open-state assertions follow an explicit simulated user
# expansion; they do not establish real-browser visibility or layout acceptance.
# Skipped: full integration/Git/lock regression and real-browser acceptance.
# Risk: native HTML, selection, focus, keyboard and layout require browser validation.
#
# plan_notes.verification (FIX-REV-2-2; preceding blocks are historical):
# Candidate compiled and collected in memory; no workspace files were written.
# Candidate: 127 passed with --capture=sys -p no:cacheprovider, no skips.
# Confirmation visibility is checked through every ancestor without manual expansion.
# Snapshot regressions check redisplay, deferred application and restored confirmation
# against decision titles, explanations, enabled controls and the primary action.
# In-memory mutation checks independently removed automatic expansion and decision
# synchronization; the corresponding regression assertions rejected both mutations.
# Skipped: full integration/Git/lock regression and real-browser acceptance.
# Risk: simulated DOM ancestry cannot establish native focus, selection or layout.
