"""
SciGate Dashboard API Server
Serves the interactive dashboard and real API for scanning repos.

    python dashboard/server.py                # serve on :8742
    python dashboard/server.py --port 9000
"""

from __future__ import annotations

import json
import sys
import time
import traceback
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

DASHBOARD_DIR = Path(__file__).parent
PROJECT_ROOT = DASHBOARD_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scigate.agents.audit import run_audit
from scigate.agents.memory import OrgMemory
from scigate.scoring.engine import compute_score
from scigate.scoring.badge import badge_url, badge_markdown

DEFAULT_MEMORY_PATH = Path.home() / ".scigate" / "org_memory.json"


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def do_POST(self):
        if self.path == "/api/scan":
            self._handle_scan()
        elif self.path == "/api/memory/stats":
            self._handle_memory_stats()
        else:
            self._json(404, {"error": "Not found"})

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def _handle_scan(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._json(400, {"error": "Invalid JSON"})
            return

        repo_path = data.get("repo_path", "").strip()
        if not repo_path or not Path(repo_path).is_dir():
            self._json(400, {"error": f"Not a directory: {repo_path}"})
            return

        try:
            t0 = time.time()
            mem = OrgMemory.load(DEFAULT_MEMORY_PATH)
            report = run_audit(repo_path, memory_hints=mem.get_hints(report.field.value) if False else [])
            sc = compute_score(report)
            elapsed = int((time.time() - t0) * 1000)

            for finding in report.findings[:10]:
                mem.record(
                    repo_pattern=finding.check_id,
                    repro_failure_type=finding.title,
                    fix_applied=finding.suggestion or "",
                    score_delta=0,
                    sci_field=report.field.value,
                )

            fixes = []
            for i, f in enumerate(sorted(report.findings, key=lambda x: -x.points_deducted)[:5], 1):
                fixes.append({
                    "rank": i,
                    "title": f.title,
                    "files": f.file_path or "multiple files",
                    "dimension": f.dimension,
                    "points": f.points_deducted,
                })

            self._json(200, {
                "score": {
                    "total_score": round(sc.total_score, 1),
                    "grade": sc.grade,
                    "field": sc.field,
                    "field_confidence": round(sc.field_confidence, 2),
                    "badge_color": sc.badge_color,
                    "dimensions": {
                        "env": round(sc.env, 1),
                        "seeds": round(sc.seeds, 1),
                        "data": round(sc.data, 1),
                        "docs": round(sc.docs, 1),
                    },
                },
                "fixes": fixes,
                "badge_url": badge_url(sc),
                "badge_markdown": badge_markdown(sc),
                "files_scanned": report.files_scanned,
                "findings_count": len(report.findings),
                "scan_ms": elapsed,
            })

            sys.stderr.write(
                f"[SciGate] Scanned {repo_path} -> {sc.total_score:.0f}/100 "
                f"({sc.field}) in {elapsed}ms\n"
            )

        except Exception as exc:
            traceback.print_exc()
            self._json(500, {"error": str(exc)})

    def _handle_memory_stats(self):
        try:
            mem = OrgMemory.load(DEFAULT_MEMORY_PATH)
            self._json(200, mem.get_stats())
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def _json(self, status: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            sys.stderr.write(f"[SciGate API] {fmt % args}\n")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8742)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    server = HTTPServer(("0.0.0.0", args.port), DashboardHandler)
    url = f"http://localhost:{args.port}"
    print(f"\n  SciGate Dashboard running at {url}")
    print(f"  API: POST {url}/api/scan  body: {{\"repo_path\": \"/path/to/repo\"}}")
    print(f"  Press Ctrl+C to stop.\n")

    if not args.no_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
