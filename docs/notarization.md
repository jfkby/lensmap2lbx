# macOS signing & notarization

The build workflow signs, notarizes, and staples `LensLabels.app`
automatically on every tag — once five repository secrets are set. Without
them it still builds an unsigned app (first launch: right-click → Open).

## One-time setup

You need a paid Apple Developer Program membership.

1. **Developer ID Application certificate.** In Xcode: Settings → Accounts →
   Manage Certificates → “+” → *Developer ID Application* (or create it at
   developer.apple.com → Certificates). Then in Keychain Access, export that
   certificate **with its private key** as a `.p12`, choosing an export
   password.

2. **Base64 the .p12** so it can live in a secret:

   ```
   base64 -i DeveloperID.p12 | pbcopy
   ```

3. **App-specific password** for notarization: sign in at
   account.apple.com → Sign-In and Security → App-Specific Passwords →
   generate one (any label, e.g. `lenslabels-notary`).

4. **Team ID**: the 10-character ID shown at developer.apple.com →
   Membership.

5. Add the secrets in the GitHub repo — Settings → Secrets and variables →
   Actions:

   | Secret                | Value                                    |
   |-----------------------|------------------------------------------|
   | `MACOS_CERT_P12`      | base64 of the exported `.p12`            |
   | `MACOS_CERT_PASSWORD` | the export password                      |
   | `APPLE_ID`            | your Apple ID email                      |
   | `APPLE_TEAM_ID`       | 10-character team ID                     |
   | `APPLE_APP_PASSWORD`  | the app-specific password                |

Push a tag (`git tag v1.0.1 && git push --tags`) and the workflow imports
the certificate into a throwaway keychain, has PyInstaller sign every
bundled binary with the hardened runtime (`assets/entitlements.plist`),
seals the outer bundle, submits to `notarytool`, waits for approval, and
staples the ticket — the released zip opens with no Gatekeeper friction.

## Signing locally instead

The same happens on your own Mac if the identity is in your login keychain:

```
export CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
pyinstaller lenslabels.spec
codesign --force --options runtime --timestamp \
         --entitlements assets/entitlements.plist \
         --sign "$CODESIGN_IDENTITY" dist/LensLabels.app
ditto -c -k --keepParent dist/LensLabels.app LensLabels.zip
xcrun notarytool submit LensLabels.zip \
      --apple-id YOU@EXAMPLE.COM --team-id TEAMID \
      --password APP-SPECIFIC-PASSWORD --wait
xcrun stapler staple dist/LensLabels.app
```

(`notarytool store-credentials` can cache those three flags as a named
profile if you notarize often.)

## Notes

- The entitlements grant `allow-unsigned-executable-memory` and
  `disable-library-validation` — the standard pair for PyInstaller apps,
  which load Python extension modules at runtime.
- The bundle identifier is `org.lensmap2lbx.LensLabels` in
  `lenslabels.spec`; change it to your own reverse-DNS string if you
  prefer. Developer ID notarization does not require the identifier to be
  pre-registered.
- Windows builds are unaffected; Authenticode signing could be added the
  same way later if ever needed.
