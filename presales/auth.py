import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt

ISSUER = 'presales-demo'
AUDIENCE = 'presales-api'
ROLES = {'viewer', 'sales', 'manager'}


@dataclass(frozen=True)
class Actor:
    subject: str
    tenant: str
    role: str

    def can_read(self, document):
        return document['tenant'] in ('*', self.tenant) and self.role in document['roles']


def local_signing_key(directory: Path):
    configured = os.getenv('PRESALES_JWT_SECRET')
    if configured:
        if len(configured) < 32:
            raise ValueError('PRESALES_JWT_SECRET must contain at least 32 characters')
        return configured
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'jwt.key'
    if not path.exists():
        try:
            with path.open('x', encoding='ascii') as f:
                f.write(secrets.token_urlsafe(48))
        except FileExistsError:
            pass
    return path.read_text(encoding='ascii').strip()


def issue_token(actor: Actor, key: str, minutes=30):
    now = datetime.now(timezone.utc)
    return jwt.encode({'sub': actor.subject, 'tenant': actor.tenant, 'role': actor.role,
                       'iss': ISSUER, 'aud': AUDIENCE, 'iat': now, 'exp': now+timedelta(minutes=minutes)},
                      key, algorithm='HS256')


def verify_token(raw: str, key: str):
    claims = jwt.decode(raw, key, algorithms=['HS256'], audience=AUDIENCE, issuer=ISSUER,
                        options={'require': ['sub', 'tenant', 'role', 'exp', 'iat']})
    if claims['role'] not in ROLES or claims['tenant'] not in {'demo-a', 'demo-b'}:
        raise jwt.InvalidTokenError('invalid identity claims')
    if not isinstance(claims['sub'], str) or not 1 <= len(claims['sub']) <= 100:
        raise jwt.InvalidTokenError('invalid subject')
    return Actor(claims['sub'], claims['tenant'], claims['role'])
