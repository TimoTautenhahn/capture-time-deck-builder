# Local Auth Hub

Zentraler lokaler OAuth-Broker fuer mehrere Mini-Apps.

## Einmaliges Setup

1. Google OAuth Desktop Client JSON nach `auth-hub/credentials.json` legen.
2. Redirect URI im Google Client erlauben:
   - `http://127.0.0.1:8790/oauth/callback`

## Start

```bash
python auth-hub/server.py
```

## API

- `GET /health`
- `GET /apps`
- `GET /oauth/start?targetApp=presentationPublisher&returnTo=http://127.0.0.1:8788`
- `GET /token?targetApp=presentationPublisher`
- `POST /token/revoke` JSON: `{ "targetApp": "presentationPublisher" }`

## Hinweise

- Tokens werden lokal in `auth-hub/state/tokens.json` gespeichert.
- `auth-hub/apps.json` steuert, welche App-ID welche Scopes bekommt.
