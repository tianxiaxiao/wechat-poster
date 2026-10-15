#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import json
import ssl
import urllib.request

APPID = os.getenv("WX_APPID", "")
SECRET = os.getenv("WX_SECRET", "")
BASE = "https://api.weixin.qq.com/cgi-bin"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def http_get(url, params=None):
    if params:
        qs = urllib.parse.urlencode(params)
        url = f"{url}?{qs}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, context=CTX, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post(url, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, context=CTX, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    r = http_get(f"{BASE}/token", {
        "grant_type": "client_credential",
        "appid": APPID,
        "secret": SECRET,
    })
    token = r["access_token"]

    rr = http_post(f"{BASE}/freepublish/batchget?access_token={token}", {
        "offset": 0,
        "count": 3,
        "no_content": 0,
    })
    print(json.dumps(rr, ensure_ascii=False, indent=2)[:6000])


if __name__ == "__main__":
    main()