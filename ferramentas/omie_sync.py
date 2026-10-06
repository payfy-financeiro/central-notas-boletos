#!/usr/bin/env python3
"""
Robô do painel "Controle de NFs por Aporte": puxa o contas a receber do Omie
(clientes, categorias, títulos, link da 2ª via do boleto e, se houver, dados da
NFS-e) e grava um arquivo criptografado que só o painel consegue abrir.

- Credenciais: variáveis de ambiente OMIE_APP_KEY e OMIE_APP_SECRET
  (no GitHub ficam em Settings > Secrets and variables > Actions).
- Criptografia: chave pública do painel (omie_chave_publica.json). O robô não
  tem a chave privada, então não consegue ler o que ele mesmo gravou.
- O repositório é público, então os logs mostram SÓ contagens e nomes de
  campos, nunca valores, nomes de clientes ou CNPJs.

Variáveis opcionais:
  OMIE_DESDE        data mínima (vencimento ou emissão) dos títulos, AAAA-MM-DD (padrão 2025-01-01)
  OMIE_MAX_BOLETOS  máximo de consultas de boleto por rodada (padrão 3000)

Uso: python omie_sync.py --saida omie.json
"""
import argparse, base64, collections, datetime, gzip, json, os, re, sys, time, urllib.error, urllib.request

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

API = os.environ.get("OMIE_API_URL") or "https://app.omie.com.br/api/v1/"
INTERVALO = float(os.environ.get("OMIE_INTERVALO") or 0.35)  # s entre chamadas (~170/min; o limite do Omie é 240/min por método)
_ultima = [0.0]


class OmieErro(Exception):
    pass


def log(msg):
    print(msg, flush=True)


def _esperar():
    falta = INTERVALO - (time.monotonic() - _ultima[0])
    if falta > 0:
        time.sleep(falta)
    _ultima[0] = time.monotonic()


def chamar(endpoint, call, param, _tentativa=0):
    """Chama a API do Omie. Devolve o JSON, ou None quando o Omie diz que não há registros."""
    corpo = json.dumps({"call": call, "app_key": KEY, "app_secret": SECRET, "param": [param]}).encode()
    _esperar()
    req = urllib.request.Request(API + endpoint, data=corpo, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(txt)
        except Exception:
            j = {}
        fs = str(j.get("faultstring", ""))
        fl = fs.lower()
        if "não existem registros" in fl or "nao existem registros" in fl:
            return None
        if e.code == 425:
            raise OmieErro(f"{call}: API bloqueada temporariamente pelo Omie (HTTP 425). Tente de novo em 30 min.")
        if ("redundante" in fl or "redundant" in fl) and _tentativa < 1:
            log(f"  {call}: consumo redundante, aguardando 61 s…")
            time.sleep(61)
            return chamar(endpoint, call, param, _tentativa + 1)
        if not fs and e.code >= 500 and _tentativa < 3:
            time.sleep([5, 15, 30][_tentativa])
            return chamar(endpoint, call, param, _tentativa + 1)
        if "chave de acesso" in fl:
            raise OmieErro(f"{call}: o Omie recusou a chave ({fs[:120]}). Confira os secrets: "
                           f"OMIE_APP_KEY tem {len(KEY)} caracteres (só dígitos: {KEY.isdigit()}), "
                           f"OMIE_APP_SECRET tem {len(SECRET)} caracteres.")
        raise OmieErro(f"{call}: HTTP {e.code} {j.get('faultcode', '')} {fs[:160]}")
    except (urllib.error.URLError, TimeoutError) as e:
        if _tentativa < 3:
            time.sleep([5, 15, 30][_tentativa])
            return chamar(endpoint, call, param, _tentativa + 1)
        raise OmieErro(f"{call}: falha de rede ({type(e).__name__})")


def paginar(endpoint, call, base, lista, pag="pagina", por="registros_por_pagina", n=500, total="total_de_paginas", limite=1000):
    p, itens, primeira = 1, [], None
    while p <= limite:
        r = chamar(endpoint, call, {**base, pag: p, por: n})
        if not r:
            break
        if primeira is None:
            primeira = r
        itens += r.get(lista) or []
        if p >= int(r.get(total) or 1):
            break
        p += 1
    return itens, primeira


# ---------- utilitários ----------
digitos = lambda s: re.sub(r"\D", "", str(s or ""))
num_nf = lambda s: digitos(s).lstrip("0")


def iso(br):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", str(br or ""))
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


def achatar(d, pre=""):
    out = {}
    for k, v in (d or {}).items():
        if isinstance(v, dict):
            out.update(achatar(v, pre + k + "."))
        else:
            out[pre + k] = v
    return out


def primeiro(flat, padrao, url=False):
    for k, v in flat.items():
        if re.search(padrao, k.split(".")[-1], re.I) and v not in (None, "", 0):
            if url and not str(v).startswith("http"):
                continue
            return str(v).strip()
    return ""


def fechado(st):
    s = (st or "").upper()
    return any(x in s for x in ("RECEB", "LIQUID", "PAGO", "CANCEL"))


def cifrar(payload, caminho_pub):
    with open(caminho_pub, encoding="utf-8") as f:
        jwk = json.load(f)
    pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    pub = ec.EllipticCurvePublicNumbers(int.from_bytes(pad(jwk["x"]), "big"), int.from_bytes(pad(jwk["y"]), "big"), ec.SECP256R1()).public_key()
    eph = ec.generate_private_key(ec.SECP256R1())
    salt, iv = os.urandom(16), os.urandom(12)
    chave = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"payfy-omie-v1").derive(eph.exchange(ec.ECDH(), pub))
    raw = gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), mtime=0)
    n = eph.public_key().public_numbers()
    b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    b64 = lambda b: base64.b64encode(b).decode()
    return {
        "v": 1, "z": 1,
        "epk": {"kty": "EC", "crv": "P-256", "x": b64u(n.x.to_bytes(32, "big")), "y": b64u(n.y.to_bytes(32, "big"))},
        "salt": b64(salt), "iv": b64(iv), "ct": b64(AESGCM(chave).encrypt(iv, raw, None)),
    }


# ---------- etapas ----------
def puxar_clientes():
    itens, _ = paginar("geral/clientes/", "ListarClientes", {"apenas_importado_api": "N"}, "clientes_cadastro")
    m = {}
    for c in itens:
        nome = (c.get("razao_social") or c.get("nome_fantasia") or "").strip()
        m[c.get("codigo_cliente_omie")] = (nome, digitos(c.get("cnpj_cpf"))[:14])
    log(f"Clientes: {len(m)}")
    return m


def puxar_categorias():
    try:
        itens, _ = paginar("geral/categorias/", "ListarCategorias", {}, "categoria_cadastro")
        m = {c.get("codigo"): (c.get("descricao") or "").strip() for c in itens if c.get("codigo")}
        log(f"Categorias: {len(m)}")
        return m
    except OmieErro as e:
        log(f"Aviso: categorias indisponíveis ({e})")
        return {}


def puxar_titulos(desde):
    itens, _ = paginar("financas/contareceber/", "ListarContasReceber", {"apenas_importado_api": "N"}, "conta_receber_cadastro")
    log(f"Títulos no Omie: {len(itens)}")
    if itens:
        log("  campos do título: " + ", ".join(sorted(itens[0].keys())))
        bol = next((t.get("boleto") for t in itens if isinstance(t.get("boleto"), dict)), None)
        if bol:
            log("  campos do boleto: " + ", ".join(sorted(bol.keys())))
    sel = [t for t in itens if max(iso(t.get("data_vencimento")), iso(t.get("data_emissao"))) >= desde]
    log(f"Títulos a partir de {desde}: {len(sel)}")
    log("  situações: " + json.dumps(collections.Counter((t.get("status_titulo") or "?") for t in sel), ensure_ascii=False))
    return sel


def puxar_boletos(titulos, maximo):
    alvo = [t for t in titulos
            if isinstance(t.get("boleto"), dict) and str(t["boleto"].get("cGerado", "")).upper() == "S"
            and not fechado(t.get("status_titulo"))]
    alvo.sort(key=lambda t: iso(t.get("data_vencimento")), reverse=True)
    if len(alvo) > maximo:
        log(f"Aviso: {len(alvo)} boletos em aberto; consultando só os {maximo} mais recentes")
        alvo = alvo[:maximo]
    links, falhas, seguidas, campos = {}, 0, 0, None
    for t in alvo:
        try:
            r = chamar("financas/contareceberboleto/", "ObterBoleto", {"nCodTitulo": t["codigo_lancamento_omie"]}) or {}
            seguidas = 0
            if campos is None:
                campos = sorted(r.keys())
                log("  campos do ObterBoleto: " + ", ".join(campos))
            flat = achatar(r)
            link = primeiro(flat, r"^cLinkBoleto$", url=True) or primeiro(flat, r"link|url", url=True)
            if link:
                links[t["codigo_lancamento_omie"]] = {"l": link, "cb": primeiro(flat, r"linha|barras|digit")}
        except OmieErro as e:
            falhas += 1
            seguidas += 1
            if falhas <= 3:
                log(f"  aviso boleto: {str(e)[:120]}")
            if seguidas >= 5:
                log("Aviso: 5 falhas seguidas no ObterBoleto; parei para não bloquear a API.")
                break
    log(f"Boletos em aberto: {len(alvo)} | links obtidos: {len(links)} | falhas: {falhas}")
    return links


def puxar_nfse(numeros):
    """Melhor esforço: se as NFS-e forem emitidas pelo Omie, guarda código de verificação/link do PDF."""
    if not numeros:
        return {}
    try:
        itens, primeira = paginar("servicos/nfse/", "ListarNFSEs", {}, "nfseEncontradas",
                                  pag="nPagina", por="nRegPorPagina", n=100, total="nTotPaginas", limite=300)
    except OmieErro as e:
        log(f"Aviso: NFS-e do Omie indisponíveis ({e}). O painel usa o link da prefeitura quando tiver o código.")
        return {}
    if primeira is not None and not itens:
        log("  NFS-e: resposta sem 'nfseEncontradas'; chaves: " + ", ".join(sorted(primeira.keys())))
    out, campos = {}, None
    for n in itens:
        flat = achatar(n)
        if campos is None:
            campos = sorted(flat.keys())
            log("  campos da NFS-e: " + ", ".join(campos))
        nf = num_nf(primeiro(flat, r"^[nc]?Num(ero)?NFSe$|^numero_?nfse$"))
        if nf in numeros:
            out[nf] = {k: v for k, v in {
                "cv": primeiro(flat, r"verif"),
                "url": primeiro(flat, r"pdf|url|link", url=True),
            }.items() if v}
    log(f"NFS-e no Omie: {len(itens)} | casadas com títulos: {len(out)} | com código de verificação: {sum(1 for v in out.values() if v.get('cv'))}")
    return out


def main():
    global KEY, SECRET
    ap = argparse.ArgumentParser()
    ap.add_argument("--saida", required=True)
    ap.add_argument("--chave-publica", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "omie_chave_publica.json"))
    a = ap.parse_args()
    KEY, SECRET = os.environ.get("OMIE_APP_KEY", "").strip(), os.environ.get("OMIE_APP_SECRET", "").strip()
    if not KEY or not SECRET:
        sys.exit("Faltam os secrets OMIE_APP_KEY e OMIE_APP_SECRET.")
    desde = os.environ.get("OMIE_DESDE") or "2025-01-01"
    maximo = int(os.environ.get("OMIE_MAX_BOLETOS") or 3000)

    try:
        clientes = puxar_clientes()
        titulos = puxar_titulos(desde)
    except OmieErro as e:
        sys.exit(f"Erro ao consultar o Omie: {e}")
    categorias = puxar_categorias()
    try:
        boletos = puxar_boletos(titulos, maximo)
    except OmieErro as e:
        log(f"Aviso: boletos interrompidos ({e})")
        boletos = {}
    nfse = puxar_nfse({num_nf(t.get("numero_documento_fiscal")) for t in titulos if num_nf(t.get("numero_documento_fiscal"))})

    cli_idx, cli_lista, cats_usadas, tit = {}, [], {}, []
    for t in titulos:
        cod = t.get("codigo_cliente_fornecedor")
        if cod not in cli_idx:
            cli_idx[cod] = len(cli_lista)
            cli_lista.append(list(clientes.get(cod, ("(cliente não encontrado no Omie)", ""))))
        cat = t.get("codigo_categoria") or next((c.get("codigo_categoria") for c in (t.get("categorias") or []) if c.get("codigo_categoria")), "")
        if cat and cat in categorias:
            cats_usadas[cat] = categorias[cat]
        b = t.get("boleto") if isinstance(t.get("boleto"), dict) else {}
        reg = {
            "id": t.get("codigo_lancamento_omie"),
            "ci": cli_idx[cod],
            "nf": num_nf(t.get("numero_documento_fiscal")),
            "doc": str(t.get("numero_documento") or "").strip(),
            "parc": str(t.get("numero_parcela") or "").strip(),
            "em": iso(t.get("data_emissao")),
            "ve": iso(t.get("data_vencimento")),
            "v": round(float(t.get("valor_documento") or 0), 2),
            "st": (t.get("status_titulo") or "").strip(),
            "cat": cat,
        }
        if str(b.get("cGerado", "")).upper() == "S":
            reg["bol"] = dict({"n": str(b.get("cNumBoleto") or "").strip()}, **boletos.get(reg["id"], {}))
        tit.append({k: v for k, v in reg.items() if v not in ("", None)})

    agora = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    payload = {"t": agora, "desde": desde, "cli": cli_lista, "cat": cats_usadas, "tit": tit, "nfse": nfse}
    os.makedirs(os.path.dirname(os.path.abspath(a.saida)), exist_ok=True)
    with open(a.saida, "w", encoding="utf-8") as f:
        json.dump(cifrar(payload, a.chave_publica), f, separators=(",", ":"))

    abertos = [x for x in tit if not fechado(x.get("st"))]
    resumo = [
        "### Sincronização Omie",
        f"- Títulos desde {desde}: **{len(tit)}** (em aberto: {len(abertos)})",
        f"- Com nº de NF: {sum(1 for x in tit if x.get('nf'))}",
        f"- Com boleto gerado: {sum(1 for x in tit if 'bol' in x)} | com link de 2ª via: {sum(1 for x in tit if x.get('bol', {}).get('l'))}",
        f"- NFS-e com código/link: {len(nfse)}",
        f"- Clientes: {len(cli_lista)}",
    ]
    log("\n".join(resumo))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n".join(resumo) + "\n")


if __name__ == "__main__":
    main()
