#!/usr/bin/env python3
"""
Ferramenta do painel "Controle de NFs por Aporte" (Payfy).

Os dados dos aportes ficam criptografados dentro do index.html:
  - ENC  = pacote de dados (JSON compactado com gzip e criptografado com AES-GCM)
           usando uma chave de dados aleatória (DEK).
  - KEYS = a mesma DEK "embrulhada" por cada senha (uma entrada por papel:
           "fin" = financeiro, acesso completo; "cs" = somente consulta).
Trocar uma senha só mexe na linha KEYS; os dados não mudam.

O pacote de dados também guarda (campo "K") a chave privada que abre o arquivo
do Omie gerado pelo robô (ferramentas/omie_sync.py). O robô só tem a chave
pública (ferramentas/omie_chave_publica.json), então não consegue ler nada.

Uso:
  python painel.py abrir   index.html --senha SENHA --saida dados.json
  python painel.py fechar  index.html dados.json --senha SENHA_FIN [--saida novo.html]
  python painel.py senha   index.html --senha SENHA_FIN --papel cs --nova NOVA_SENHA
  python painel.py gerar-senha
  python painel.py migrar  index.html --senha SENHA_ATUAL --senha-cs SENHA_CS   (uso único: v1 -> v2)

Requer: pip install cryptography
"""
import argparse, base64, gzip, json, os, re, secrets, sys

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITER = 200_000
ALFABETO = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # sem 0/O, 1/I/L

b64e = lambda b: base64.b64encode(b).decode()
b64d = lambda s: base64.b64decode(s)
b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()


# ---------- leitura/escrita das linhas ENC e KEYS no HTML ----------
def ler_html(caminho):
    with open(caminho, encoding="utf-8") as f:
        return f.read().split("\n")


def achar_linha(linhas, prefixo):
    for i, l in enumerate(linhas):
        if l.startswith(prefixo):
            return i
    return -1


def ler_const(linhas, nome):
    i = achar_linha(linhas, f"const {nome}=")
    if i < 0:
        return None, -1
    txt = linhas[i][len(f"const {nome}="):].rstrip()
    if txt.endswith(";"):
        txt = txt[:-1]
    return json.loads(txt), i


def gravar_html(caminho, linhas):
    with open(caminho, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(linhas))


# ---------- cripto ----------
def kek_de(senha, salt, it):
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=it).derive(senha.encode())


def embrulhar(dek, senha, papel):
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(kek_de(senha, salt, ITER)).encrypt(iv, dek, None)
    return {"r": papel, "iter": ITER, "salt": b64e(salt), "iv": b64e(iv), "ct": b64e(ct)}


def desembrulhar(keys, senha):
    for k in keys:
        try:
            dek = AESGCM(kek_de(senha, b64d(k["salt"]), k["iter"])).decrypt(b64d(k["iv"]), b64d(k["ct"]), None)
            return dek, k["r"]
        except Exception:
            continue
    sys.exit("Senha incorreta.")


def cifrar_pacote(dek, payload):
    iv = os.urandom(12)
    raw = gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), mtime=0)
    return {"v": 2, "z": 1, "iv": b64e(iv), "ct": b64e(AESGCM(dek).encrypt(iv, raw, None))}


def decifrar_pacote(dek, enc):
    pt = AESGCM(dek).decrypt(b64d(enc["iv"]), b64d(enc["ct"]), None)
    if enc.get("z"):
        pt = gzip.decompress(pt)
    return json.loads(pt)


def nova_senha():
    g = lambda: "".join(secrets.choice(ALFABETO) for _ in range(4))
    return "-".join(g() for _ in range(4))


def par_omie():
    """Gera o par de chaves P-256 do Omie: privada (JWK, vai para o pacote) e pública (JWK, vai para o robô)."""
    priv = ec.generate_private_key(ec.SECP256R1())
    n = priv.private_numbers()
    x = n.public_numbers.x.to_bytes(32, "big")
    y = n.public_numbers.y.to_bytes(32, "big")
    d = n.private_value.to_bytes(32, "big")
    pub = {"kty": "EC", "crv": "P-256", "x": b64u(x), "y": b64u(y)}
    return dict(pub, d=b64u(d)), pub


# ---------- comandos ----------
def cmd_abrir(a):
    linhas = ler_html(a.html)
    enc, _ = ler_const(linhas, "ENC")
    keys, _ = ler_const(linhas, "KEYS")
    if keys is None:  # formato antigo (v1): senha direto no pacote
        key = kek_de(a.senha, b64d(enc["salt"]), enc["iter"])
        payload = json.loads(AESGCM(key).decrypt(b64d(enc["iv"]), b64d(enc["ct"]), None))
        papel = "v1"
    else:
        dek, papel = desembrulhar(keys, a.senha)
        payload = decifrar_pacote(dek, enc)
    with open(a.saida, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"ok ({papel}) -> {a.saida}")


def cmd_fechar(a):
    linhas = ler_html(a.html)
    keys, _ = ler_const(linhas, "KEYS")
    _, i_enc = ler_const(linhas, "ENC")
    dek, papel = desembrulhar(keys, a.senha)
    if papel != "fin":
        sys.exit("Use a senha do financeiro.")
    with open(a.dados, encoding="utf-8") as f:
        payload = json.load(f)
    if "K" not in payload:
        sys.exit("O pacote não tem a chave do Omie (campo K). Abra o original com 'abrir' e edite a partir dele.")
    linhas[i_enc] = "const ENC=" + json.dumps(cifrar_pacote(dek, payload), separators=(",", ":")) + ";"
    gravar_html(a.saida or a.html, linhas)
    print("ok ->", a.saida or a.html)


def cmd_senha(a):
    linhas = ler_html(a.html)
    keys, i_keys = ler_const(linhas, "KEYS")
    dek, papel = desembrulhar(keys, a.senha)
    if papel != "fin":
        sys.exit("Use a senha do financeiro.")
    nova = a.nova or nova_senha()
    keys = [k for k in keys if k["r"] != a.papel] + [embrulhar(dek, nova, a.papel)]
    linhas[i_keys] = "const KEYS=" + json.dumps(keys, separators=(",", ":")) + ";"
    gravar_html(a.saida or a.html, linhas)
    print(f"ok: senha '{a.papel}' = {nova}")


def cmd_migrar(a):
    linhas = ler_html(a.html)
    enc, i_enc = ler_const(linhas, "ENC")
    if achar_linha(linhas, "const KEYS=") >= 0:
        sys.exit("Este arquivo já está no formato novo.")
    key = kek_de(a.senha, b64d(enc["salt"]), enc["iter"])
    payload = json.loads(AESGCM(key).decrypt(b64d(enc["iv"]), b64d(enc["ct"]), None))
    priv, pub = par_omie()
    payload["K"] = priv
    dek = AESGCM.generate_key(256)
    keys = [embrulhar(dek, a.senha, "fin"), embrulhar(dek, a.senha_cs, "cs")]
    linhas[i_enc] = "const ENC=" + json.dumps(cifrar_pacote(dek, payload), separators=(",", ":")) + ";"
    linhas.insert(i_enc + 1, "const KEYS=" + json.dumps(keys, separators=(",", ":")) + ";")
    gravar_html(a.saida or a.html, linhas)
    with open(a.chave_publica, "w", encoding="utf-8") as f:
        json.dump(pub, f, indent=2)
    print("ok ->", a.saida or a.html, "| chave pública ->", a.chave_publica)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)
    x = s.add_parser("abrir"); x.add_argument("html"); x.add_argument("--senha", required=True); x.add_argument("--saida", required=True)
    x = s.add_parser("fechar"); x.add_argument("html"); x.add_argument("dados"); x.add_argument("--senha", required=True); x.add_argument("--saida")
    x = s.add_parser("senha"); x.add_argument("html"); x.add_argument("--senha", required=True); x.add_argument("--papel", required=True, choices=["fin", "cs"]); x.add_argument("--nova"); x.add_argument("--saida")
    s.add_parser("gerar-senha")
    x = s.add_parser("migrar"); x.add_argument("html"); x.add_argument("--senha", required=True); x.add_argument("--senha-cs", required=True); x.add_argument("--saida"); x.add_argument("--chave-publica", default="omie_chave_publica.json")
    a = p.parse_args()
    if a.cmd == "gerar-senha":
        print(nova_senha())
        return
    {"abrir": cmd_abrir, "fechar": cmd_fechar, "senha": cmd_senha, "migrar": cmd_migrar}[a.cmd](a)


if __name__ == "__main__":
    main()
