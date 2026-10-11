import shutil, os

BASE = "D:/ai-agent-project"
DST = "D:/personal-website/deep/agent"
items = [
    ("phase2-rag/README.md", "phase2-rag.md"),
    ("phase3-agent/README.md", "phase3-agent.md"),
    ("phase4-agent-ops/README.md", "phase4-agent-ops.md"),
    ("phase5-mcp-client/README.md", "phase5-mcp-client.md"),
    ("phase5-mcp-server/README.md", "phase5-mcp-server.md"),
    ("phase6-eval/README.md", "phase6-eval.md"),
    ("rdx-agent/README.md", "rdx-agent.md"),
]
for s, d in items:
    shutil.copy(os.path.join(BASE, s), os.path.join(DST, d))
print("copied", len(items), "files")
print("agent dir file count:", len([f for f in os.listdir(DST) if not f.startswith('.')]))
