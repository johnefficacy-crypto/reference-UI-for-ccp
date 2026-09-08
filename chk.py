import json, urllib.request, os
base = os.environ["CCP_API_BASE"]; tok = os.environ["CCP_ADMIN_JWT"]
for pid, qid in [
    ("afde97ed-a07c-4c3c-87b1-b7a184c310e2", "38742898-cdc3-4264-8012-19c784f62db1"),
    ("69dc2dfa-e70e-4fc5-b08c-d29573b041f2", "18bc9d24-7b4d-4ef9-9c02-9dd01c07a1cb"),
    ("a8476f1b-1422-4c9c-9279-74661254b791", "b195d4bd-bc6a-45ba-94f1-20ed30b08740"),
]:
    req = urllib.request.Request(f"{base}/api/admin/exam-intelligence-cms/pyq-questions?pyq_paper_id={pid}&limit=50",
                                 headers={"Authorization": "Bearer " + tok})
    d = json.load(urllib.request.urlopen(req))
    t = [x for x in d["items"] if x["id"] == qid][0]["question_text"]
    print(qid[:8], repr(t[:120]))
