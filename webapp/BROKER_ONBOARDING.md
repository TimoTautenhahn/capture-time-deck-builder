# Broker Onboarding fuer neue Apps

Diese Webapp nutzt den Hosted OAuth Broker als zentrale Auth- und Publish-Schicht.

## Pflichtschritte bei jeder neuen App

1. App zentral im Broker registrieren:
   - Datei: `server/config/oauth-apps.json` (im Broker-Projekt)
   - Felder: `appId`, `appName`, `scopes`, `defaultReturnTo`, `allowedReturnToPrefixes`
2. Sicherstellen, dass `returnTo` der neuen App von `allowedReturnToPrefixes` abgedeckt ist.
3. App ueber Broker-Endpunkt `/apps` sichtbar machen und lokal pruefen (`Apps anzeigen` in der Mini-App).
4. OAuth-End-to-End testen:
   - `Mit Auth-Hub verbinden`
   - Ruecksprung auf `/auth/callback`
   - `Auth-Status pruefen` -> `tokenReady = true`
5. Publish-Test ausfuehren (`Upload + Praesentation erstellen`).

## Callback-Strategie

- Kanonische Broker-Redirect-URI: `/oauth/callback`
- Lokale Apps starten Auth nur ueber `/oauth/start` und erhalten den Ruecksprung ueber `returnTo`.
