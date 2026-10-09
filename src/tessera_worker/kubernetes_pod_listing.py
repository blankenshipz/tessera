"""Bounded namespace discovery; only exact Job-owned Pods become evidence."""

from urllib.parse import urlencode


def list_owned_pods(client, job_uid: str, *, max_pages: int = 100) -> list[dict]:
    if not isinstance(job_uid, str) or not job_uid:
        raise ValueError("Pod discovery requires a Job UID")
    owned, uids, names, tokens = [], set(), set(), set()
    token = None
    version = None
    for _ in range(max_pages):
        query = {"limit": 100}
        if token:
            query["continue"] = token
        page = client.request("GET", client.path("pod") + "?" + urlencode(query))
        if (
            not isinstance(page, dict)
            or page.get("apiVersion") != "v1"
            or page.get("kind") != "PodList"
            or not isinstance(page.get("metadata"), dict)
            or not isinstance(page.get("items"), list)
            or len(page["items"]) > 100
        ):
            raise ValueError("Pod listing is not a bounded typed page")
        current_version = page["metadata"].get("resourceVersion")
        if version is not None and current_version != version:
            raise ValueError("Pod listing snapshot changed")
        version = current_version
        for pod in page["items"]:
            metadata = pod.get("metadata") if isinstance(pod, dict) else None
            refs = metadata.get("ownerReferences", []) if isinstance(metadata, dict) else []
            if not isinstance(refs, list) or not any(
                isinstance(ref, dict) and ref.get("kind") == "Job" and ref.get("uid") == job_uid
                for ref in refs
            ):
                continue
            if pod.get("apiVersion", "v1") != "v1" or pod.get("kind", "Pod") != "Pod":
                raise ValueError("Pod list contains an invalid resource type")
            uid, name = metadata.get("uid"), metadata.get("name")
            if not all(isinstance(value, str) and value for value in (uid, name)):
                raise ValueError("Owned Pod identity is missing")
            if uid in uids or name in names:
                raise ValueError("Owned Pod identity was replayed or replaced")
            uids.add(uid)
            names.add(name)
            owned.append({**pod, "apiVersion": "v1", "kind": "Pod"})
            if len(owned) > 128:
                raise ValueError("Owned Pod set exceeds discovery limit")
        token = page["metadata"].get("continue", "")
        if not isinstance(token, str) or len(token) > 16384:
            raise ValueError("Pod continuation token is invalid")
        if not token:
            return owned
        if token in tokens:
            raise ValueError("Pod continuation token was replayed")
        tokens.add(token)
    raise ValueError("Pod listing exceeds continuation limit")
