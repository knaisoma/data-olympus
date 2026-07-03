"""Self-contained HTML graph visualizer for data-olympus bundles.

Adapted from the OKF reference viewer (Apache-2.0) in
GoogleCloudPlatform/knowledge-catalog; see NOTICE for the full attribution.
This module adapts the approach to the data_olympus.format.Document model,
uses a flat node/edge JSON shape, and inlines the template as a Python string
so no external asset files are required.

Bundle body HTML is sanitized with DOMPurify before rendering.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from data_olympus.format import Document
from data_olympus.format.validate import RESERVED as _RESERVED

_SKIP = frozenset({".git", "__pycache__", ".venv", ".pytest_cache", ".ruff_cache", "node_modules"})
_LINK_RE = re.compile(r"\]\((/?[^)\s#]+\.md)(?:#[^)]*)?\)")
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")


def _strip_code(body: str) -> str:
    """Remove fenced and inline code spans so links inside them are ignored."""
    return _INLINE_CODE_RE.sub(" ", _FENCE_RE.sub(" ", body))

# Template uses __DISPLAY_NAME__ and __JSON__ as substitution placeholders (no str.format).
# JS curly braces are therefore raw, no escaping needed.
_HTML_TEMPLATE = (
    "<!DOCTYPE html>\n"
    '<html lang="en">\n'
    "<head>\n"
    '<meta charset="utf-8">\n'
    "<title>__DISPLAY_NAME__ - data-olympus viewer</title>\n"
    '<script src="https://cdn.jsdelivr.net/npm/cytoscape@3.28.1/dist/cytoscape.min.js"'
    ' integrity="sha384-J7Q85oZE4GJ/e7+n2aOQsLXfDwwfnA8S2nZAL5BpFsfpCF84zQD7LroZ/dMnLgex"'
    ' crossorigin="anonymous"></script>\n'
    '<script src="https://cdn.jsdelivr.net/npm/marked@12.0.0/marked.min.js"'
    ' integrity="sha384-NNQgBjjuhtXzPmmy4gurS5X7P4uTt1DThyevz4Ua0IVK5+kazYQI1W27JHjbbxQz"'
    ' crossorigin="anonymous"></script>\n'
    '<script src="https://cdn.jsdelivr.net/npm/dompurify@3.0.11/dist/purify.min.js"'
    ' integrity="sha384-Ic7KEGROu37YaruU6NyiYeib7UhjFyDZQ5fzBAji965L75T/4LGk5nzwMEjNGexs"'
    ' crossorigin="anonymous"></script>\n'
    "<style>\n"
    "* { box-sizing: border-box; margin: 0; padding: 0; }\n"
    "body { font-family: system-ui, sans-serif; display: flex; flex-direction: column;"
    " height: 100vh; overflow: hidden; background: #0f172a; color: #e2e8f0; }\n"
    "header { padding: 0.75rem 1rem; background: #1e293b; display: flex; align-items: center;"
    " gap: 1rem; border-bottom: 1px solid #334155; flex-shrink: 0; }\n"
    "header strong { color: #f8fafc; font-size: 1.1rem; }\n"
    ".muted { color: #94a3b8; font-size: 0.85rem; }\n"
    ".controls { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-left: auto; }\n"
    ".controls input, .controls select, .controls button { background: #0f172a; color: #e2e8f0;"
    " border: 1px solid #334155; border-radius: 0.375rem; padding: 0.25rem 0.5rem;"
    " font-size: 0.8rem; }\n"
    ".controls button:hover { background: #1e293b; cursor: pointer; }\n"
    "main { display: flex; flex: 1; overflow: hidden; }\n"
    "#graph { flex: 1; background: #0f172a; }\n"
    "#detail { width: 360px; overflow-y: auto; background: #1e293b;"
    " border-left: 1px solid #334155; padding: 1rem; flex-shrink: 0; }\n"
    ".type-chip { display: inline-block; font-size: 0.7rem; padding: 0.15rem 0.5rem;"
    " border-radius: 9999px; background: #334155; color: #94a3b8; margin-bottom: 0.4rem;"
    " text-transform: uppercase; letter-spacing: 0.05em; }\n"
    "h1 { font-size: 1.1rem; color: #f8fafc; margin-bottom: 0.2rem; }\n"
    ".detail-id { font-size: 0.75rem; color: #64748b;"
    " margin-bottom: 0.75rem; font-family: monospace; }\n"
    "dl.frontmatter { font-size: 0.82rem; margin-bottom: 0.75rem; }\n"
    "dl.frontmatter dt { color: #64748b; margin-top: 0.4rem; }\n"
    "dl.frontmatter dd { color: #cbd5e1; }\n"
    "hr { border: none; border-top: 1px solid #334155; margin: 0.75rem 0; }\n"
    "#detail-body { font-size: 0.85rem; line-height: 1.6; color: #cbd5e1; }\n"
    "#detail-body a { color: #60a5fa; }\n"
    "#detail-backlinks h2 { font-size: 0.9rem; color: #94a3b8; margin-top: 0.75rem; }\n"
    "#detail-backlinks ul { padding-left: 1rem; font-size: 0.82rem; }\n"
    "#detail-backlinks li { margin-top: 0.25rem; }\n"
    "#detail-backlinks a { color: #60a5fa; text-decoration: none; }\n"
    "</style>\n"
    "</head>\n"
    "<body>\n"
    "<header>\n"
    '  <strong id="bundle-name"></strong>\n'
    '  <span class="muted">data-olympus bundle</span>\n'
    '  <div class="controls">\n'
    '    <input id="search" type="search" placeholder="Search title / id">\n'
    '    <select id="filter-type"><option value="">All types</option></select>\n'
    '    <select id="layout">\n'
    '      <option value="cose">cose (force)</option>\n'
    '      <option value="concentric">concentric</option>\n'
    '      <option value="breadthfirst">breadth-first</option>\n'
    '      <option value="circle">circle</option>\n'
    '      <option value="grid">grid</option>\n'
    "    </select>\n"
    '    <button id="reset">Reset view</button>\n'
    "  </div>\n"
    "</header>\n"
    "<main>\n"
    '  <section id="graph"></section>\n'
    '  <section id="detail">\n'
    '    <div id="detail-empty" class="muted">Click a node to see details.</div>\n'
    '    <article id="detail-content" hidden>\n'
    "      <header>\n"
    '        <span class="type-chip" id="detail-type"></span>\n'
    '        <h1 id="detail-title"></h1>\n'
    '        <div class="detail-id" id="detail-id"></div>\n'
    "      </header>\n"
    '      <dl class="frontmatter">\n'
    "        <dt>Description</dt><dd id=\"detail-desc\"></dd>\n"
    "      </dl>\n"
    "      <hr>\n"
    '      <div id="detail-body"></div>\n'
    '      <section id="detail-backlinks" hidden>\n'
    "        <h2>Cited by</h2>\n"
    '        <ul id="backlinks-list"></ul>\n'
    "      </section>\n"
    "    </article>\n"
    "  </section>\n"
    "</main>\n"
    "<script>\n"
    "window.__BUNDLE_DATA__ = __JSON__;\n"
    "</script>\n"
    "<script>\n"
    "(function() {\n"
    "  var D = window.__BUNDLE_DATA__;\n"
    "  document.getElementById('bundle-name').textContent = D.name || '';\n"
    "\n"
    "  var backlinks = {};\n"
    "  D.edges.forEach(function(e) {\n"
    "    if (!backlinks[e.target]) backlinks[e.target] = [];\n"
    "    backlinks[e.target].push(e.source);\n"
    "  });\n"
    "\n"
    "  var types = [...new Set(D.nodes.map(function(n) { return n.type; }))].sort();\n"
    "  var sel = document.getElementById('filter-type');\n"
    "  types.forEach(function(t) {\n"
    "    var o = document.createElement('option');"
    " o.value = t; o.textContent = t; sel.appendChild(o);\n"
    "  });\n"
    "\n"
    '  var palette = {"standard":"#3b82f6","decision":"#8b5cf6","workflow":"#10b981",'
    '"project":"#f59e0b","memory":"#ec4899","reference":"#6b7280"};\n'
    "  function nodeColor(t) { return palette[t] || '#94a3b8'; }\n"
    "\n"
    "  var cy = cytoscape({\n"
    "    container: document.getElementById('graph'),\n"
    "    elements: [\n"
    "      ...D.nodes.map(function(n) {\n"
    "        return { data: { id: n.id, label: n.title || n.id, type: n.type,"
    " color: nodeColor(n.type) } };\n"
    "      }),\n"
    "      ...D.edges.map(function(e) {\n"
    "        return { data: { id: e.source+'__'+e.target, source: e.source, target: e.target } };\n"
    "      })\n"
    "    ],\n"
    "    style: [\n"
    "      { selector: 'node', style: {\n"
    "        'background-color': 'data(color)',\n"
    "        'label': 'data(label)',\n"
    "        'color': '#f8fafc',\n"
    "        'font-size': 11,\n"
    "        'text-valign': 'bottom',\n"
    "        'text-margin-y': 4,\n"
    "        'width': 28, 'height': 28\n"
    "      } },\n"
    "      { selector: 'edge', style: {\n"
    "        'line-color': '#334155',\n"
    "        'target-arrow-color': '#334155',\n"
    "        'target-arrow-shape': 'triangle',\n"
    "        'curve-style': 'bezier',\n"
    "        'width': 1.5\n"
    "      } },\n"
    "      { selector: ':selected', style: { 'border-width': 3, 'border-color': '#f8fafc' } }\n"
    "    ],\n"
    "    layout: { name: 'cose', animate: false }\n"
    "  });\n"
    "\n"
    "  cy.on('tap', 'node', function(evt) {\n"
    "    var n = D.nodes.find(function(x) { return x.id === evt.target.id(); });\n"
    "    if (!n) return;\n"
    "    document.getElementById('detail-empty').hidden = true;\n"
    "    var c = document.getElementById('detail-content');\n"
    "    c.hidden = false;\n"
    "    document.getElementById('detail-type').textContent = n.type;\n"
    "    document.getElementById('detail-title').textContent = n.title || n.id;\n"
    "    document.getElementById('detail-id').textContent = n.id;\n"
    "    document.getElementById('detail-desc').textContent = n.description || '';\n"
    "    var body = D.bodies && D.bodies[n.id] || '';\n"
    "    var rawHtml = marked.parse ? marked.parse(body)"
    " : (typeof marked === 'function' ? marked(body) : body);\n"
    "    document.getElementById('detail-body').innerHTML = DOMPurify.sanitize(rawHtml);\n"
    "    var bl = document.getElementById('detail-backlinks');\n"
    "    var bll = document.getElementById('backlinks-list');\n"
    "    var srcs = backlinks[n.id] || [];\n"
    "    if (srcs.length) {\n"
    "      bl.hidden = false;\n"
    "      bll.textContent = '';\n"
    "      srcs.forEach(function(s) {\n"
    "        var sn = D.nodes.find(function(x) { return x.id === s; });\n"
    "        var li = document.createElement('li');\n"
    "        var a = document.createElement('a');\n"
    "        a.href = '#';\n"
    "        a.dataset.id = s;\n"
    "        a.textContent = sn ? sn.title : s;\n"
    "        li.appendChild(a);\n"
    "        bll.appendChild(li);\n"
    "      });\n"
    "    } else { bl.hidden = true; }\n"
    "  });\n"
    "\n"
    "  document.getElementById('detail').addEventListener('click', function(e) {\n"
    "    var a = e.target.closest('[data-id]');\n"
    "    if (!a) return;\n"
    "    e.preventDefault();\n"
    "    cy.$('#'+CSS.escape(a.dataset.id)).trigger('tap');\n"
    "  });\n"
    "\n"
    "  document.getElementById('search').addEventListener('input', function(e) {\n"
    "    var q = e.target.value.toLowerCase();\n"
    "    cy.nodes().forEach(function(n) {\n"
    "      var match = !q || n.data('label').toLowerCase().includes(q)"
    " || n.id().toLowerCase().includes(q);\n"
    "      n.style('opacity', match ? 1 : 0.15);\n"
    "    });\n"
    "  });\n"
    "\n"
    "  document.getElementById('filter-type').addEventListener('change', function(e) {\n"
    "    var t = e.target.value;\n"
    "    cy.nodes().forEach(function(n) {\n"
    "      n.style('opacity', !t || n.data('type') === t ? 1 : 0.15);\n"
    "    });\n"
    "  });\n"
    "\n"
    "  document.getElementById('layout').addEventListener('change', function(e) {\n"
    "    cy.layout({ name: e.target.value, animate: false }).run();\n"
    "  });\n"
    "\n"
    "  document.getElementById('reset').addEventListener('click', function() {\n"
    "    cy.fit(); cy.zoom(1);\n"
    "  });\n"
    "})();\n"
    "</script>\n"
    "</body>\n"
    "</html>\n"
)


def _concept_id(md_path: Path, root: Path) -> str:
    """Derive concept id: path relative to root without .md extension, POSIX separators."""
    return md_path.relative_to(root).with_suffix("").as_posix()


def _extract_links(body: str, doc_path: Path, root: Path, known_ids: set[str]) -> list[str]:
    """Extract internal .md links from body; return target concept ids in known_ids."""
    out: list[str] = []
    seen: set[str] = set()
    root_resolved = root.resolve()
    for m in _LINK_RE.finditer(_strip_code(body)):
        target = m.group(1)
        if "://" in target:
            continue
        if target.startswith("/"):
            # absolute path relative to bundle root
            candidate = root / target.lstrip("/")
        else:
            candidate = doc_path.parent / target
        try:
            rel = candidate.resolve().relative_to(root_resolved)
        except ValueError:
            continue
        concept_id = rel.with_suffix("").as_posix()
        if concept_id in known_ids and concept_id not in seen:
            seen.add(concept_id)
            out.append(concept_id)
    return out


def generate_visualization(
    root: Path,
    out_path: Path,
    name: str | None = None,
) -> dict[str, Any]:
    """Walk a bundle and write a self-contained HTML visualization.

    Returns: {"nodes": n, "edges": m, "bytes": k}.
    """
    root = Path(root)
    out_path = Path(out_path)
    if not root.is_dir():
        raise FileNotFoundError(f"Bundle directory not found: {root}")

    display_name = name or root.resolve().name

    # First pass: collect all concept paths and their ids
    md_files: list[Path] = []
    for md in sorted(root.rglob("*.md")):
        if any(part in _SKIP for part in md.parts):
            continue
        if md.name in _RESERVED:
            continue
        md_files.append(md)

    known_ids: set[str] = {_concept_id(p, root) for p in md_files}

    # Second pass: build nodes
    nodes: list[dict[str, Any]] = []
    bodies: dict[str, str] = {}
    links_by_id: dict[str, list[str]] = {}

    for md in md_files:
        cid = _concept_id(md, root)
        try:
            doc = Document.load(md)
        except Exception:  # noqa: BLE001
            continue
        fm = doc.frontmatter
        title = str(fm.get("title") or cid)
        doc_type = str(fm.get("type") or "concept")
        desc = str(fm.get("description") or "")
        body = doc.body or ""
        nodes.append({"id": cid, "title": title, "type": doc_type, "description": desc})
        bodies[cid] = body
        links_by_id[cid] = _extract_links(body, md, root, known_ids)

    # Build edges
    node_ids = {n["id"] for n in nodes}
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str]] = set()
    for src_id, targets in links_by_id.items():
        for tgt_id in targets:
            if tgt_id == src_id or tgt_id not in node_ids:
                continue
            key = (src_id, tgt_id)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append({"source": src_id, "target": tgt_id})

    bundle_data = {
        "name": display_name,
        "nodes": nodes,
        "edges": edges,
        "bodies": bodies,
    }

    # Escape any ``</`` (item 7): the JSON payload is embedded inside a
    # ``<script>`` block, and the HTML parser ends that block at the first literal
    # ``</script>`` REGARDLESS of JSON/string context. A doc body containing
    # ``</script><script>alert(1)</script>`` would otherwise break out and run
    # arbitrary JS. ``<\/`` is an equivalent JSON escape (JSON permits ``\/``), so
    # the data is unchanged after JSON.parse but the HTML parser never sees a
    # closing tag.
    json_str = json.dumps(bundle_data).replace("</", "<\\/")
    safe_name = html.escape(display_name)
    # Single-pass substitution (item 7): replacing __JSON__ then __DISPLAY_NAME__
    # (or vice versa) let payload content that happened to contain the OTHER
    # placeholder get mangled by the second pass — e.g. a doc body with the literal
    # ``__DISPLAY_NAME__`` was rewritten to the bundle name. Substituting both in
    # one regex pass means neither replacement's output is rescanned.
    _subs = {"__JSON__": json_str, "__DISPLAY_NAME__": safe_name}
    html_out = re.sub(
        "__JSON__|__DISPLAY_NAME__", lambda m: _subs[m.group(0)], _HTML_TEMPLATE
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_out, encoding="utf-8")

    return {
        "nodes": len(nodes),
        "edges": len(edges),
        "bytes": len(html_out.encode("utf-8")),
    }
