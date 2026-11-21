#!/usr/bin/env python3
"""Serve HTML documentation for an EDIABAS .prg file, built from parse_prg's JSON.

Usage: python3 -I serve_book.py --file ECU/BDC_G05.prg [--port 8000]
Routes: /  /jobs  /job/<name>  /tables  /table/<name>  /api/json
"""
import argparse
import html
import http.server
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_prg import parse_prg  # noqa: E402

CSS = """
body{margin:0;font:15px/1.5 system-ui,sans-serif;display:flex;min-height:100vh;color:#222}
nav{width:200px;background:#1f2933;color:#fff;padding:16px;position:sticky;top:0;height:100vh;box-sizing:border-box}
nav a{display:block;color:#cbd2d9;text-decoration:none;padding:4px 0}nav a:hover{color:#fff}
nav h2{font-size:15px;margin:0 0 12px;word-break:break-all}
main{flex:1;padding:24px 32px;min-width:0}
table{border-collapse:collapse;font-size:13px;display:block;overflow-x:auto;max-width:100%}
th,td{border:1px solid #ccd;padding:4px 8px;text-align:left;vertical-align:top}
th{background:#eef}
pre{background:#f6f8fa;padding:12px;overflow-x:auto;font-size:13px}
code{background:#f0f0f0;padding:1px 4px;border-radius:3px}
summary{cursor:pointer;font-size:1.2em;margin:16px 0}
input#f{width:100%;max-width:420px;padding:6px 10px;margin:8px 0 16px;font-size:14px}
@media(prefers-color-scheme:dark){body{background:#12161b;color:#d8dee4}th{background:#222a35}
pre,code{background:#1c232c}th,td{border-color:#38424f}a{color:#6cb6ff}}
"""
JS = """
const f=document.getElementById('f');
if(f)f.oninput=()=>{const q=f.value.toLowerCase();
document.querySelectorAll('main table tr').forEach((r,i)=>{if(i)r.style.display=
r.textContent.toLowerCase().includes(q)?'':'none'})}
"""

e = html.escape


def q(name):
    return urllib.parse.quote(name, safe="")


def table(headers, rows):
    return "<table><tr>%s</tr>%s</table>" % (
        "".join("<th>%s</th>" % e(str(h)) for h in headers),
        "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in r) for r in rows))


def overview(d):
    g = d["global_comments"]
    v = d["version"]
    return ("<h1>%s</h1>" % e(g.get("ECU", "ECU")) +
            "<h2>Description</h2>" + table(["Field", "Value"], [[e(k), e(val)] for k, val in g.items()]) +
            "<h2>Version block</h2>" + table(["Field", "Value"], [[e(k), e(str(val))] for k, val in v.items()]) +
            "<h2>Statistics</h2><ul><li>File size: %d bytes</li><li>Jobs: %d</li><li>Tables: %d</li>"
            "<li>Table rows: %d</li><li>Uses: %s</li></ul>" % (
                d["size"], len(d["jobs"]), len(d["tables"]), sum(len(t["rows"]) for t in d["tables"]),
                e(", ".join(d["uses"]) or "none")) +
            "<h2>Header pointers</h2>" + table(["Offset", "Value"], [[e(k), "0x%X" % val] for k, val in d["header"].items()]))


def jobs_index(d):
    return "<h1>Jobs</h1>" + table(
        ["Job", "Description", "Args", "Results", "Instr."],
        [['<a href="/job/%s">%s</a>' % (q(j["name"]), e(j["name"])),
          e(" ".join(d["job_info"].get(j["name"], {}).get("comment", []))),
          len(d["job_info"].get(j["name"], {}).get("args", [])),
          len(d["job_info"].get(j["name"], {}).get("results", [])), len(j["code"])] for j in d["jobs"]])


def items_table(items, kind):
    if not items:
        return "<p>none</p>"
    keys = []
    for it in items:
        keys += [k for k in it if k not in keys]
    cell = lambda v: e("\n".join(v) if isinstance(v, list) else str(v))  # noqa: E731
    return table([k for k in keys], [[cell(it.get(k, "")) for k in keys] for it in items])


def job_page(d, name):
    j = next((j for j in d["jobs"] if j["name"] == name), None)
    if not j:
        return None
    info = d["job_info"].get(name, {"comment": [], "args": [], "results": [], "other": {}})
    targets = {c["target"] for c in j["code"] if c["target"] is not None}
    asm = []
    for c in j["code"]:
        if c["addr"] in targets:
            asm.append("loc_%08X:" % c["addr"])
        asm.append("  %08X: %s" % (c["addr"], e(c["text"])))
    return ("<h1>Job <code>%s</code></h1><p>Address <code>0x%X</code></p>" % (e(name), j["address"]) +
            "<p>%s</p>" % e(" ".join(info["comment"])) +
            "<h2>Arguments</h2>" + items_table(info["args"], "arg") +
            "<h2>Results</h2>" + items_table(info["results"], "result") +
            ("<h2>Other</h2>" + table(["Key", "Value"], [[e(k), e("\n".join(v))] for k, v in info["other"].items()])
             if info["other"] else "") +
            "<details><summary><b>Disassembly</b> (%d instructions)</summary><pre>%s</pre></details>"
            % (len(j["code"]), "\n".join(asm)))


def tables_index(d):
    return "<h1>Tables</h1>" + table(
        ["Table", "Columns", "Rows"],
        [['<a href="/table/%s">%s</a>' % (q(t["name"]), e(t["name"])), len(t["columns"]), len(t["rows"])]
         for t in d["tables"]])


def table_page(d, name):
    by_name = {t["name"].upper(): t["name"] for t in d["tables"]}
    t = next((t for t in d["tables"] if t["name"].upper() == name.upper()), None)
    if not t:
        return None

    def cell(c):
        target = by_name.get(c.strip().upper())
        return '<a href="/table/%s">%s</a>' % (q(target), e(c)) if target else e(c)

    return "<h1>Table <code>%s</code></h1>" % e(t["name"]) + table(t["columns"], [[cell(c) for c in r] for r in t["rows"]])


def make_handler(data, title):
    nav = ('<nav><h2>%s</h2><a href="/">Overview</a><a href="/jobs">Jobs</a><a href="/tables">Tables</a>'
           '<a href="/api/json">JSON</a></nav>' % e(title))

    class H(http.server.BaseHTTPRequestHandler):
        def send(self, code, body, ctype="text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
            if path == "/api/json":
                return self.send(200, json.dumps(data).encode(), "application/json")
            body = None
            if path == "/":
                body = overview(data)
            elif path == "/jobs":
                body = jobs_index(data)
            elif path == "/tables":
                body = tables_index(data)
            elif path.startswith("/job/"):
                body = job_page(data, path[5:])
            elif path.startswith("/table/"):
                body = table_page(data, path[7:])
            if body is None:
                return self.send(404, b"not found", "text/plain")
            box = '<input id="f" placeholder="Filter rows..." autofocus>' if "<table>" in body else ""
            page = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                    "<title>%s</title><style>%s</style>%s<main>%s%s</main><script>%s</script>"
                    % (e(title), CSS, nav, box, body, JS))
            self.send(200, page.encode("utf-8"))

        def log_message(self, *a):
            pass

    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", required=True, help="path to .prg file")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    data = parse_prg(a.file)
    print("Parsed %d jobs, %d tables. Serving at http://127.0.0.1:%d" % (len(data["jobs"]), len(data["tables"]), a.port))
    http.server.ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(data, os.path.basename(a.file))).serve_forever()


if __name__ == "__main__":
    main()
