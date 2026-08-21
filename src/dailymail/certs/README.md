# Vendored CA material

## `incommon-rsa-server-ca-2.pem`

`apps.rowan.edu` serves an **incomplete certificate chain**: it presents only its
leaf certificate and omits the `InCommon RSA Server CA 2` intermediate. Stock
HTTPS clients therefore fail with "unable to get local issuer certificate"
(OpenSSL verify code 21). See `docs/site-reconnaissance.md` §2.5.

The fix is to supply the missing intermediate, **not** to disable verification.

This file is a **public intermediate CA certificate** — not a secret, not a
credential. It is the same certificate published at the leaf's Authority
Information Access URI.

| Property | Value |
|---|---|
| Subject | `C=US, O=Internet2, CN=InCommon RSA Server CA 2` |
| Issuer | `C=US, ST=New Jersey, L=Jersey City, O=The USERTRUST Network, CN=USERTrust RSA Certification Authority` |
| Serial | `835B7615206D2D6E097E0B6E409FEFC0` |
| Not after | `Nov 15 23:59:59 2032 GMT` |
| SHA-256 fingerprint | `87:E0:1C:C4:DD:0C:9D:92:A3:DB:D4:90:92:FF:13:F9:CD:38:74:45:CD:C5:7E:5B:98:4E:1B:77:21:B5:B0:29` |
| Source | `http://crt.sectigo.com/InCommonRSAServerCA2.crt` (AIA of the `apps.rowan.edu` leaf) |
| Retrieved | 2026-08-21 |

Its issuer (`USERTrust RSA Certification Authority`) is a normal public root
present in `certifi` and in the system trust store, so trust still terminates at
a standard root. This file only bridges the gap Rowan's server leaves open.

To re-verify or refresh:

```sh
curl -sS -o incommon.der http://crt.sectigo.com/InCommonRSAServerCA2.crt
openssl x509 -inform DER -in incommon.der -out incommon-rsa-server-ca-2.pem
openssl x509 -in incommon-rsa-server-ca-2.pem -noout -subject -serial -fingerprint -sha256
```
