#!/usr/bin/env python3
"""
Sign up or sign in with Supabase Auth (REST), then call Django GET /core/auth/me/ with the JWT.

Requires backend SUPABASE_JWT_SECRET set so Django can verify the Bearer token, and either an
existing Django user for that email or SUPABASE_AUTO_PROVISION_DJANGO_USER=true (default).

Environment:
  SUPABASE_URL              e.g. https://xxxxx.supabase.co
  SUPABASE_ANON_OR_PUBLISH  anon JWT or sb_publishable_... (Dashboard → API)
  DJANGO_API_BASE           default http://127.0.0.1:8000/api/v1

Examples (PowerShell):
  $env:SUPABASE_URL="https://xxxxx.supabase.co"
  $env:SUPABASE_ANON_OR_PUBLISH="sb_publishable_..."
  $env:DJANGO_API_BASE="http://127.0.0.1:8000/api/v1"
  python scripts/try_supabase_auth_me.py --signup user@example.com 'SecretPass123!'
  python scripts/try_supabase_auth_me.py user@example.com 'SecretPass123!'
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Tuple


def _post_json(url: str, headers: Dict[str, str], body: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode()), resp.status


def _get_json(url: str, headers: Dict[str, str]) -> Tuple[Dict[str, Any], int]:
    req = urllib.request.Request(url, headers=headers, method='GET')
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode()), resp.status


def _extract_access_token(payload: Dict[str, Any]) -> str | None:
    if isinstance(payload.get('access_token'), str):
        return payload['access_token']
    sess = payload.get('session')
    if isinstance(sess, dict) and isinstance(sess.get('access_token'), str):
        return sess['access_token']
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Supabase Auth + Django /core/auth/me/ smoke test (stdlib only).'
    )
    parser.add_argument('email')
    parser.add_argument('password')
    parser.add_argument(
        '--signup',
        action='store_true',
        help='POST /auth/v1/signup instead of password grant',
    )
    args = parser.parse_args()

    base = os.environ.get('SUPABASE_URL', '').rstrip('/')
    key = os.environ.get('SUPABASE_ANON_OR_PUBLISH', '').strip()
    api_base = os.environ.get('DJANGO_API_BASE', 'http://127.0.0.1:8000/api/v1').rstrip('/')

    if not base or not key:
        print(
            'Missing env: SUPABASE_URL and SUPABASE_ANON_OR_PUBLISH',
            file=sys.stderr,
        )
        return 2

    headers = {
        'apikey': key,
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
    }
    email = args.email.strip().lower()

    if args.signup:
        auth_url = f'{base}/auth/v1/signup'
        body: dict = {'email': email, 'password': args.password}
    else:
        auth_url = f'{base}/auth/v1/token?grant_type=password'
        body = {'email': email, 'password': args.password}

    try:
        data, _status = _post_json(auth_url, headers, body)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors='replace')
        print(f'Supabase Auth HTTP {e.code}:\n{err_body}', file=sys.stderr)
        return 1

    token = _extract_access_token(data)
    if not token:
        print('No access_token in Supabase response:', json.dumps(data, indent=2)[:4000], file=sys.stderr)
        return 1

    me_url = f'{api_base}/core/auth/me/'
    me_headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
    try:
        me_data, me_status = _get_json(me_url, me_headers)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors='replace')
        print(f'Django GET /core/auth/me/ HTTP {e.code}:\n{err_body}', file=sys.stderr)
        return 1

    print(json.dumps({'status': me_status, 'me': me_data}, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
