/* Minimal DOM stub: just enough of the API that app.js touches, so the
   rendering logic can be exercised without a browser. Not a general shim. */

"use strict";

function ClassList(node) {
  this.node = node;
}
ClassList.prototype.contains = function (name) {
  return this.node.className.split(/\s+/).indexOf(name) >= 0;
};
ClassList.prototype.add = function (name) {
  if (!this.contains(name)) {
    this.node.className = (this.node.className + " " + name).trim();
  }
};
ClassList.prototype.remove = function (name) {
  this.node.className = this.node.className
    .split(/\s+/)
    .filter(function (value) { return value && value !== name; })
    .join(" ");
};

function Element(tag) {
  this.tagName = String(tag).toUpperCase();
  this.className = "";
  this.style = {};
  this.children = [];
  this.parentNode = null;
  this.listeners = {};
  this.classList = new ClassList(this);
  this._text = "";
  this.colSpan = null;
  this.offsetWidth = 0; // read by the client to force an animation restart
  this.attributes = {};
}

Element.prototype.setAttribute = function (name, value) {
  this.attributes[name] = String(value);
  // SVG nodes carry their class through the attribute, not the property.
  if (name === "class") this.className = String(value);
};
Element.prototype.getAttribute = function (name) {
  return Object.prototype.hasOwnProperty.call(this.attributes, name)
    ? this.attributes[name]
    : null;
};
Element.prototype.insertBefore = function (child, reference) {
  if (child.parentNode) {
    var at = child.parentNode.children.indexOf(child);
    if (at >= 0) child.parentNode.children.splice(at, 1);
  }
  this._text = "";
  var index = reference ? this.children.indexOf(reference) : -1;
  if (index < 0) this.children.push(child);
  else this.children.splice(index, 0, child);
  child.parentNode = this;
  return child;
};

Object.defineProperty(Element.prototype, "textContent", {
  get: function () {
    if (!this.children.length) return this._text;
    return this.children.map(function (child) { return child.textContent; }).join("");
  },
  set: function (value) {
    this.children.forEach(function (child) { child.parentNode = null; });
    this.children = [];
    this._text = String(value);
  },
});

Element.prototype.appendChild = function (child) {
  if (child.parentNode) {
    var at = child.parentNode.children.indexOf(child);
    if (at >= 0) child.parentNode.children.splice(at, 1);
  }
  this._text = "";
  this.children.push(child);
  child.parentNode = this;
  return child;
};

Element.prototype.removeChild = function (child) {
  var at = this.children.indexOf(child);
  if (at >= 0) this.children.splice(at, 1);
  child.parentNode = null;
  return child;
};

Element.prototype.addEventListener = function (name, handler) {
  (this.listeners[name] = this.listeners[name] || []).push(handler);
};

Element.prototype.dispatch = function (name) {
  (this.listeners[name] || []).forEach(function (handler) { handler(); });
};

Element.prototype.querySelector = function (selector) {
  var wanted = selector.replace(/^\./, "");
  for (var i = 0; i < this.children.length; i += 1) {
    if (this.children[i].className.split(/\s+/).indexOf(wanted) >= 0) return this.children[i];
  }
  return null;
};

function Document() {
  this.byId = Object.create(null);
}
Document.prototype.createElement = function (tag) {
  return new Element(tag);
};
Document.prototype.createTextNode = function (text) {
  var node = new Element("#text");
  node._text = String(text);
  return node;
};
Document.prototype.createElementNS = function (ns, tag) {
  var node = new Element(tag);
  node.namespace = ns;
  return node;
};
Document.prototype.getElementById = function (id) {
  if (!this.byId[id]) this.byId[id] = new Element("div");
  return this.byId[id];
};

module.exports = { Document: Document, Element: Element };
