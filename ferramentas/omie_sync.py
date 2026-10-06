#!/usr/bin/env python3
"""
Robô do painel "Controle de NFs por Aporte": puxa o contas a receber do Omie
(clientes, categorias, títulos, link da 2ª via do boleto e código de verificação
das NFS-e) e grava um arquivo criptografado que só o painel consegue abrir.

- Credenciais: variáveis de ambiente OMIE_APP_KEY e OMIE_APP_SECRET
  (no GitHub ficam em Settings > Secrets and variables > Actions).
- Criptografia do resultado: chave pública do painel (omie_chave_publica.json).
  O robô não tem a chave privada, então não consegue ler o omie.json.
- Cache entre rodadas (cache.json): links de boleto e códigos das NFS-e já
  consultados, criptografados com uma chave derivada do OMIE_APP_SECRET. Assim a
  1ª rodada é longa e as seguintes só buscam o que é novo.
- O repositório é público, então os logs mostram SÓ contagens e nomes de
  campos, nunca valores, nomes de clientes ou CNPJs.

Variáveis opcionais:
  OMIE_DESDE        data mínima (vencimento ou emissão) dos títulos, AAAA-MM-DD (padrão 2025-01-01)
  OMIE_MAX_BOLETOS  máximo de consultas novas de boleto por rodada (padrão 3000)

Uso: python omie_sync.py --saida omie.json [--cache-entrada cache.json] [--cache-saida cache.json]
"""
import argparse, base64, collections, concurrent.futures, datetime, gzip, json, os, re, sys, threading, time
import urllib.error, urllib.request

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

API = os.environ.get("OMIE_API_URL") or "https://app.omie.com.br/api/v1/"
INTERVALO = float(os.environ.get("OMIE_INTERVALO") or 0.3)  # s entre disparos (limite do Omie: 240/min por método)
TRABALHADORES = 3  # chamadas simultâneas (limite do Omie: 4 por método)
_trava = threading.Lock()
_ultima = [0.0]


class OmieErro(Exception):
    pass


def log(msg):
    print(msg, flush=True)


def _esperar():
    with _trava:
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
            time.sleep(61)
            return chamar(endpoint, call, param, _tentativa + 1)
        if not fs and e.code >= 500 and _tentativa < 3:
            time.sleep([5, 15, 30][_tentativa])
            return chamar(endpoint, call, param, _tentativa + 1)
        if "chave de acesso" in fl:
            raise OmieErro(f"{call}: o Omie recusou a chave ({fs[:120]}). Confira os secrets OMIE_APP_KEY e OMIE_APP_SECRET.")
        raise OmieErro(f"{call}: HTTP {e.code} {j.get('faultcode', '')} {fs[:160]}")
    except (urllib.error.URLError, TimeoutError) as e:
        if _tentativa < 3:
            time.sleep([5, 15, 30][_tentativa])
            return chamar(endpoint, call, param, _tentativa + 1)
        raise OmieErro(f"{call}: falha de rede ({type(e).__name__})")


def paginar(endpoint, call, base, lista, pag="pagina", por="registros_por_pagina", n=500, total="total_de_paginas", paralelo=False):
    """Lê todas as páginas. Com paralelo=True, depois da 1ª página busca as demais em paralelo."""
    r = chamar(endpoint, call, {**base, pag: 1, por: n})
    if not r:
        return [], None
    itens, tot = list(r.get(lista) or []), int(r.get(total) or 1)
    if tot <= 1:
        return itens, r
    paginas = range(2, tot + 1)
    if not paralelo:
        for p in paginas:
            rr = chamar(endpoint, call, {**base, pag: p, por: n})
            itens += (rr or {}).get(lista) or []
        return itens, r
    falhas = []
    def uma(p):
        try:
            return (chamar(endpoint, call, {**base, pag: p, por: n}) or {}).get(lista) or []
        except OmieErro as e:
            falhas.append(str(e))
            return []
    with concurrent.futures.ThreadPoolExecutor(TRABALHADORES) as ex:
        for res in ex.map(uma, paginas):
            itens += res
    if falhas:
        log(f"  aviso: {len(falhas)} página(s) de {call} falharam; ex.: {falhas[0][:120]}")
    return itens, r


# ---------- utilitários ----------
digitos = lambda s: re.sub(r"\D", "", str(s or ""))
num_nf = lambda s: digitos(s).lstrip("0")


def iso(br):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", str(br or ""))
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


def br(iso_):
    a, m, d = iso_.split("-")
    return f"{d}/{m}/{a}"


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


b64 = lambda b: base64.b64encode(b).decode()
b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()


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
    return {
        "v": 1, "z": 1,
        "epk": {"kty": "EC", "crv": "P-256", "x": b64u(n.x.to_bytes(32, "big")), "y": b64u(n.y.to_bytes(32, "big"))},
        "salt": b64(salt), "iv": b64(iv), "ct": b64(AESGCM(chave).encrypt(iv, raw, None)),
    }


# ---------- cache entre rodadas (só o robô lê) ----------
def _chave_cache():
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"payfy-omie-cache", info=b"cache-v1").derive(SECRET.encode())


def ler_cache(caminho):
    vazio = {"nfse": {}, "nfse_ate": "", "bol": {}}
    if not caminho or not os.path.exists(caminho) or os.path.getsize(caminho) == 0:
        log("Cache: nenhum (primeira rodada ou cache novo)")
        return vazio
    try:
        with open(caminho, encoding="utf-8") as f:
            box = json.load(f)
        c = json.loads(gzip.decompress(AESGCM(_chave_cache()).decrypt(base64.b64decode(box["iv"]), base64.b64decode(box["ct"]), None)))
        log(f"Cache: {len(c.get('nfse', {}))} NFS-e e {len(c.get('bol', {}))} boletos já conhecidos")
        return {**vazio, **c}
    except Exception:
        log("Cache: ilegível (chave mudou?); refazendo do zero")
        return vazio


def gravar_cache(caminho, cache):
    if not caminho:
        return
    iv = os.urandom(12)
    raw = gzip.compress(json.dumps(cache, separators=(",", ":")).encode(), mtime=0)
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump({"v": 1, "iv": b64(iv), "ct": b64(AESGCM(_chave_cache()).encrypt(iv, raw, None))}, f)


# ---------- etapas ----------
def puxar_clientes():
    itens, _ = paginar("geral/clientes/", "ListarClientes", {"apenas_importado_api": "N"}, "clientes_cadastro", paralelo=True)
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
    itens, _ = paginar("financas/contareceber/", "ListarContasReceber", {"apenas_importado_api": "N"}, "conta_receber_cadastro", paralelo=True)
    log(f"Títulos no Omie: {len(itens)}")
    vistos, unicos = set(), []
    for t in itens:  # páginas em paralelo podem repetir registros se algo mudar no meio
        if t.get("codigo_lancamento_omie") not in vistos:
            vistos.add(t.get("codigo_lancamento_omie"))
            unicos.append(t)
    sel = [t for t in unicos if max(iso(t.get("data_vencimento")), iso(t.get("data_emissao"))) >= desde]
    log(f"Títulos a partir de {desde}: {len(sel)}")
    log("  situações: " + json.dumps(collections.Counter((t.get("status_titulo") or "?") for t in sel), ensure_ascii=False))
    return sel


def puxar_boletos(titulos, maximo, cache):
    alvo = [t for t in titulos
            if isinstance(t.get("boleto"), dict) and str(t["boleto"].get("cGerado", "")).upper() == "S"
            and not fechado(t.get("status_titulo"))]
    chave = lambda t: f"{t.get('data_vencimento')}|{t['boleto'].get('cNumBoleto')}|{t.get('valor_documento')}"
    links, novos = {}, []
    for t in alvo:
        c = cache["bol"].get(str(t["codigo_lancamento_omie"]))
        if c and c.get("k") == chave(t) and c.get("l"):
            links[t["codigo_lancamento_omie"]] = {"l": c["l"], "cb": c.get("cb", "")}
        else:
            novos.append(t)
    novos.sort(key=lambda t: iso(t.get("data_vencimento")), reverse=True)
    if len(novos) > maximo:
        log(f"Aviso: {len(novos)} boletos novos; consultando só os {maximo} mais recentes nesta rodada")
        novos = novos[:maximo]
    falhas, parar = [], threading.Event()
    def um(t):
        if parar.is_set():
            return None
        try:
            r = chamar("financas/contareceberboleto/", "ObterBoleto", {"nCodTitulo": t["codigo_lancamento_omie"]}) or {}
        except OmieErro as e:
            falhas.append(str(e))
            if len(falhas) >= 10:
                parar.set()
            return None
        flat = achatar(r)
        link = primeiro(flat, r"^cLinkBoleto$", url=True) or primeiro(flat, r"link|url", url=True)
        return (t, link, primeiro(flat, r"^cCodBarras$") or primeiro(flat, r"linha|barras|digit")) if link else None
    with concurrent.futures.ThreadPoolExecutor(TRABALHADORES) as ex:
        for res in ex.map(um, novos):
            if res:
                t, link, cb = res
                links[t["codigo_lancamento_omie"]] = {"l": link, "cb": cb}
                cache["bol"][str(t["codigo_lancamento_omie"])] = {"k": chave(t), "l": link, "cb": cb}
    if falhas:
        log(f"  aviso boleto: {falhas[0][:120]}")
    if parar.is_set():
        log("Aviso: muitas falhas no ObterBoleto; parei para não bloquear a API.")
    abertos = {str(t["codigo_lancamento_omie"]) for t in alvo}
    cache["bol"] = {k: v for k, v in cache["bol"].items() if k in abertos}  # só guarda boletos ainda em aberto
    log(f"Boletos em aberto: {len(alvo)} | do cache: {len(alvo) - len(novos)} | consultados agora: {len(novos)} | com link: {len(links)} | falhas: {len(falhas)}")
    return links


def puxar_nfse(desde, cache):
    """Códigos de verificação das NFS-e emitidas pelo Omie (incremental pelo cache)."""
    hoje = datetime.date.today()
    if cache.get("nfse_ate"):
        ini = (datetime.date.fromisoformat(cache["nfse_ate"]) - datetime.timedelta(days=10)).isoformat()
    else:
        ini = desde
    base = {"dEmiInicial": br(ini), "dEmiFinal": br((hoje + datetime.timedelta(days=1)).isoformat())}
    try:
        itens, primeira = paginar("servicos/nfse/", "ListarNFSEs", base, "nfseEncontradas",
                                  pag="nPagina", por="nRegPorPagina", n=100, total="nTotPaginas", paralelo=True)
    except OmieErro as e:
        log(f"Aviso: NFS-e do Omie indisponíveis ({e}). Usando só o cache.")
        return cache["nfse"]
    campos, novas = None, 0
    for n in itens:
        flat = achatar(n)
        if campos is None:
            campos = sorted(flat.keys())
        nf = num_nf(primeiro(flat, r"^nNumeroNFSe$") or primeiro(flat, r"^[nc]?Num(ero)?NFSe$"))
        cv = primeiro(flat, r"^cCodigoVerifNFSe$") or primeiro(flat, r"verif")
        if nf and cv:
            if nf not in cache["nfse"]:
                novas += 1
            ent = {"cv": cv}
            im = digitos(primeiro(flat, r"^cIMEmissor$"))
            if im and im != "74086367":
                ent["im"] = im
            cache["nfse"][nf] = ent
    cache["nfse_ate"] = hoje.isoformat()
    log(f"NFS-e consultadas desde {ini}: {len(itens)} | novas: {novas} | total no cache: {len(cache['nfse'])}")
    return cache["nfse"]


def main():
    global KEY, SECRET
    ap = argparse.ArgumentParser()
    ap.add_argument("--saida", required=True)
    ap.add_argument("--cache-entrada")
    ap.add_argument("--cache-saida")
    ap.add_argument("--chave-publica", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "omie_chave_publica.json"))
    a = ap.parse_args()
    KEY, SECRET = os.environ.get("OMIE_APP_KEY", "").strip(), os.environ.get("OMIE_APP_SECRET", "").strip()
    if not KEY or not SECRET:
        sys.exit("Faltam os secrets OMIE_APP_KEY e OMIE_APP_SECRET.")
    desde = os.environ.get("OMIE_DESDE") or "2025-01-01"
    maximo = int(os.environ.get("OMIE_MAX_BOLETOS") or 3000)
    t0 = time.monotonic()
    cache = ler_cache(a.cache_entrada)

    try:
        clientes = puxar_clientes()
        titulos = puxar_titulos(desde)
    except OmieErro as e:
        sys.exit(f"Erro ao consultar o Omie: {e}")
    categorias = puxar_categorias()
    try:
        boletos = puxar_boletos(titulos, maximo, cache)
    except OmieErro as e:
        log(f"Aviso: boletos interrompidos ({e})")
        boletos = {}
    todas_nfse = puxar_nfse(desde, cache)
    gravar_cache(a.cache_saida, cache)

    cli_idx, cli_lista, cats_usadas, tit, nfse = {}, [], {}, [], {}
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
        if reg["nf"] and reg["nf"] in todas_nfse:
            nfse[reg["nf"]] = todas_nfse[reg["nf"]]
        if str(b.get("cGerado", "")).upper() == "S":
            reg["bol"] = dict({"n": str(b.get("cNumBoleto") or "").strip()}, **boletos.get(reg["id"], {}))
        tit.append({k: v for k, v in reg.items() if v not in ("", None)})

    agora = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    payload = {"t": agora, "desde": desde, "cli": cli_lista, "cat": cats_usadas, "tit": tit, "nfse": nfse}
    os.makedirs(os.path.dirname(os.path.abspath(a.saida)), exist_ok=True)
    with open(a.saida, "w", encoding="utf-8") as f:
        json.dump(cifrar(payload, a.chave_publica), f, separators=(",", ":"))

    abertos = [x for x in tit if not fechado(x.get("st"))]
    com_nf = [x for x in tit if x.get("nf")]
    resumo = [
        "### Sincronização Omie",
        f"- Títulos desde {desde}: **{len(tit)}** (em aberto: {len(abertos)})",
        f"- Com nº de NF: {len(com_nf)} | com código de verificação: {sum(1 for x in com_nf if x['nf'] in nfse)}",
        f"- Com boleto gerado: {sum(1 for x in tit if 'bol' in x)} | com link de 2ª via: {sum(1 for x in tit if x.get('bol', {}).get('l'))}",
        f"- Clientes: {len(cli_lista)}",
        f"- Tempo: {int(time.monotonic() - t0)} s",
    ]
    log("\n".join(resumo))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n".join(resumo) + "\n")


if __name__ == "__main__":
    main()
