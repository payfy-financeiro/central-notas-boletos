#!/usr/bin/env python3
"""
Robô da "Central de Notas e Boletos" (Payfy): puxa o contas a receber do Omie
(clientes, categorias, títulos, link da 2ª via do boleto e código de verificação
das NFS-e) e grava no Supabase, de onde o painel lê.

Variáveis de ambiente:
  OMIE_APP_KEY, OMIE_APP_SECRET   chave da API do Omie (secrets do GitHub)
  SUPABASE_URL, SUPABASE_KEY      endereço e chave pública do projeto (no workflow)
  SUPABASE_ROBO_EMAIL             usuário do robô (no workflow)
  SUPABASE_ROBO_SENHA             senha do usuário do robô (secret do GitHub)
  OMIE_DESDE                      data mínima (vencimento ou emissão) dos títulos, AAAA-MM-DD (padrão 2025-01-01)
  OMIE_MAX_BOLETOS                máximo de consultas novas de boleto por rodada (padrão 3000)

O que já foi consultado (links de boleto, códigos das NFS-e) fica no próprio
Supabase, então só a 1ª rodada é longa. O repositório é público: os logs
mostram só contagens e nomes de campos, nunca valores, clientes ou CNPJs.
"""
import collections, concurrent.futures, datetime, json, os, re, sys, threading, time, uuid
import urllib.error, urllib.parse, urllib.request

API = os.environ.get("OMIE_API_URL") or "https://app.omie.com.br/api/v1/"
INTERVALO = float(os.environ.get("OMIE_INTERVALO") or 0.3)  # s entre disparos (limite do Omie: 240/min por método)
TRABALHADORES = 3  # chamadas simultâneas onde o Omie deixa (ListarNFSEs); contas a receber e boletos vão um por vez
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
        instavel = not fs or "internal error" in fl or "soap-error" in fl or "timeout" in fl
        if e.code >= 500 and instavel and _tentativa < 3:  # instabilidade do Omie: tenta de novo
            time.sleep([10, 30, 60][_tentativa])
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



# ---------- Supabase ----------
class Supa:
    def __init__(self, url, chave, email, senha):
        self.url, self.chave = url.rstrip("/"), chave
        r = self._req("POST", "/auth/v1/token?grant_type=password", {"email": email, "password": senha}, auth=False)
        self.token = r["access_token"]

    def _req(self, metodo, caminho, corpo=None, auth=True, extra=None, _tentativa=0):
        h = {"apikey": self.chave, "Content-Type": "application/json"}
        if auth:
            h["Authorization"] = "Bearer " + self.token
        h.update(extra or {})
        dados = json.dumps(corpo).encode() if corpo is not None else None
        req = urllib.request.Request(self.url + caminho, data=dados, method=metodo, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                txt = r.read().decode()
                return json.loads(txt) if txt else None
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
            if e.code >= 500 and _tentativa < 3:
                time.sleep([3, 10, 30][_tentativa])
                return self._req(metodo, caminho, corpo, auth, extra, _tentativa + 1)
            if caminho.startswith("/auth/"):
                raise SystemExit(f"Supabase recusou o login do robô (HTTP {e.code}). Confira o secret SUPABASE_ROBO_SENHA.")
            raise SystemExit(f"Erro no Supabase {metodo} {caminho.split('?')[0]}: HTTP {e.code} {txt[:200]}")
        except (urllib.error.URLError, TimeoutError) as e:
            if _tentativa < 3:
                time.sleep([3, 10, 30][_tentativa])
                return self._req(metodo, caminho, corpo, auth, extra, _tentativa + 1)
            raise SystemExit(f"Falha de rede com o Supabase ({type(e).__name__})")

    def ler_tudo(self, tabela, select, filtro=""):
        out, ini, passo = [], 0, 1000
        while True:
            lote = self._req("GET", f"/rest/v1/{tabela}?select={select}{filtro}&order=1&offset={ini}&limit={passo}".replace("order=1", "order=" + select.split(",")[0]))
            out += lote or []
            if not lote or len(lote) < passo:
                return out
            ini += passo

    def gravar(self, tabela, linhas, lote=1000):
        for i in range(0, len(linhas), lote):
            self._req("POST", f"/rest/v1/{tabela}", linhas[i:i + lote], extra={"Prefer": "resolution=merge-duplicates,return=minimal"})

    def apagar(self, tabela, filtro):
        self._req("DELETE", f"/rest/v1/{tabela}?{filtro}", extra={"Prefer": "return=minimal"})

    def meta(self):
        return {m["chave"]: m["valor"] for m in self._req("GET", "/rest/v1/meta?select=chave,valor") or []}

    def gravar_meta(self, pares):
        self.gravar("meta", [{"chave": k, "valor": v, "atualizado_em": agora_iso()} for k, v in pares.items()])


def agora_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def ler_cache_supabase(sb):
    m = sb.meta()
    bol = {str(r["id"]): {"k": r["bol_chave"], "l": r["bol_link"], "cb": r.get("bol_cb") or ""}
           for r in sb.ler_tudo("omie_titulos", "id,bol_chave,bol_link,bol_cb", "&bol_link=not.is.null")}
    log(f"Já no Supabase: {len(bol)} links de boleto | NFS-e consultadas até {m.get('nfse_ate') or '—'}")
    return {"nfse": {}, "nfse_ate": m.get("nfse_ate") or "", "nfse_desde": m.get("nfse_desde") or "9999", "bol": bol}



# ---------- etapas no Omie ----------
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
    # ListarContasReceber só aceita uma requisição por vez (erro Client-8020 em paralelo)
    itens, _ = paginar("financas/contareceber/", "ListarContasReceber", {"apenas_importado_api": "N"}, "conta_receber_cadastro")
    log(f"Títulos no Omie: {len(itens)}")
    vistos, unicos = set(), []
    for t in itens:  # páginas em paralelo podem repetir registros se algo mudar no meio
        if t.get("codigo_lancamento_omie") not in vistos:
            vistos.add(t.get("codigo_lancamento_omie"))
            unicos.append(t)
    # entra tudo que ainda está em aberto (mesmo vencido há muito tempo) + o histórico a partir de OMIE_DESDE
    sel = [t for t in unicos if not fechado(t.get("status_titulo"))
           or max(iso(t.get("data_vencimento")), iso(t.get("data_emissao"))) >= desde]
    log(f"Títulos em aberto + histórico desde {desde}: {len(sel)}")
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
    falhas, parar, sem_link = [], threading.Event(), []
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
        if not link:
            sem_link.append(str(r.get("cDesStatus") or r.get("faultstring") or "")[:100])
            return None
        return (t, link, primeiro(flat, r"^cCodBarras$") or primeiro(flat, r"linha|barras|digit"))
    with concurrent.futures.ThreadPoolExecutor(1) as ex:  # ObterBoleto: um por vez
        for res in ex.map(um, novos):
            if res:
                t, link, cb = res
                links[t["codigo_lancamento_omie"]] = {"l": link, "cb": cb}
                cache["bol"][str(t["codigo_lancamento_omie"])] = {"k": chave(t), "l": link, "cb": cb}
    if falhas:
        log(f"  aviso boleto: {falhas[0][:120]}")
    if sem_link:
        log(f"  {len(sem_link)} boleto(s) sem link; motivos: " + json.dumps(collections.Counter(sem_link).most_common(3), ensure_ascii=False))
    if parar.is_set():
        log("Aviso: muitas falhas no ObterBoleto; parei para não bloquear a API.")
    abertos = {str(t["codigo_lancamento_omie"]) for t in alvo}
    cache["bol"] = {k: v for k, v in cache["bol"].items() if k in abertos}  # só guarda boletos ainda em aberto
    log(f"Boletos em aberto: {len(alvo)} | do cache: {len(alvo) - len(novos)} | consultados agora: {len(novos)} | com link: {len(links)} | falhas: {len(falhas)}")
    return links


def puxar_nfse(desde, cache):
    """Códigos de verificação das NFS-e emitidas pelo Omie (incremental pelo cache)."""
    hoje = datetime.date.today()
    # NFs de títulos que vencem a partir de OMIE_DESDE podem ter sido emitidas até ~2 meses antes
    inicio = (datetime.date.fromisoformat(desde) - datetime.timedelta(days=62)).isoformat()
    if cache.get("nfse_ate") and cache.get("nfse_desde", "9999") <= inicio:
        ini = (datetime.date.fromisoformat(cache["nfse_ate"]) - datetime.timedelta(days=10)).isoformat()
    else:
        ini = inicio  # primeira rodada ou janela maior que a do cache: busca tudo desde o início
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
        # A prefeitura só aceita o código sem hífen (o Omie às vezes manda "XNQU-VDVL").
        cv = re.sub(r"[^0-9A-Za-z]", "", str(cv or "")).upper()
        if nf and cv:
            if nf not in cache["nfse"]:
                novas += 1
            ent = {"cv": cv}
            im = digitos(primeiro(flat, r"^cIMEmissor$"))
            if im and im != "74086367":
                ent["im"] = im
            cache["nfse"][nf] = ent
    cache["nfse_ate"] = hoje.isoformat()
    cache["nfse_desde"] = min(cache.get("nfse_desde", "9999"), ini)
    log(f"NFS-e consultadas desde {ini}: {len(itens)} | novas: {novas} | total no cache: {len(cache['nfse'])}")
    return cache["nfse"]

def main():
    global KEY, SECRET
    KEY, SECRET = os.environ.get("OMIE_APP_KEY", "").strip(), os.environ.get("OMIE_APP_SECRET", "").strip()
    if not KEY or not SECRET:
        sys.exit("Faltam os secrets OMIE_APP_KEY e OMIE_APP_SECRET.")
    faltando = [v for v in ("SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_ROBO_EMAIL", "SUPABASE_ROBO_SENHA") if not os.environ.get(v, "").strip()]
    if faltando:
        sys.exit("Faltam as variáveis: " + ", ".join(faltando))
    desde = os.environ.get("OMIE_DESDE") or "2025-01-01"
    maximo = int(os.environ.get("OMIE_MAX_BOLETOS") or 3000)
    t0 = time.monotonic()
    sb = Supa(os.environ["SUPABASE_URL"].strip(), os.environ["SUPABASE_KEY"].strip(),
              os.environ["SUPABASE_ROBO_EMAIL"].strip(), os.environ["SUPABASE_ROBO_SENHA"].strip())
    cache = ler_cache_supabase(sb)

    try:
        clientes = puxar_clientes()
        titulos = puxar_titulos(desde)
    except OmieErro as e:
        sys.exit(f"Erro ao consultar o Omie: {e}")
    if not titulos:
        sys.exit("O Omie não devolveu nenhum título; nada foi alterado no Supabase.")
    categorias = puxar_categorias()
    try:
        boletos = puxar_boletos(titulos, maximo, cache)
    except OmieErro as e:
        log(f"Aviso: boletos interrompidos ({e})")
        boletos = {}
    nfse_novas = puxar_nfse(desde, cache)

    sinc = uuid.uuid4().hex[:12]
    chave_bol = lambda t: f"{t.get('data_vencimento')}|{t['boleto'].get('cNumBoleto')}|{t.get('valor_documento')}"
    linhas = []
    for t in titulos:
        nome, cnpj = clientes.get(t.get("codigo_cliente_fornecedor"), ("(cliente não encontrado no Omie)", ""))
        cat = t.get("codigo_categoria") or next((c.get("codigo_categoria") for c in (t.get("categorias") or []) if c.get("codigo_categoria")), "")
        b = t.get("boleto") if isinstance(t.get("boleto"), dict) else {}
        gerado = str(b.get("cGerado", "")).upper() == "S"
        lk = boletos.get(t.get("codigo_lancamento_omie"), {})
        linhas.append({
            "id": t.get("codigo_lancamento_omie"),
            "cliente": nome,
            "cnpj": cnpj,
            "nf": num_nf(t.get("numero_documento_fiscal")) or None,
            "doc": str(t.get("numero_documento") or "").strip() or None,
            "parc": str(t.get("numero_parcela") or "").strip() or None,
            "emissao": iso(t.get("data_emissao")) or None,
            "vencimento": iso(t.get("data_vencimento")) or None,
            "valor": round(float(t.get("valor_documento") or 0), 2),
            "status": (t.get("status_titulo") or "").strip(),
            "categoria": categorias.get(cat, "") or None,
            "bol_num": (str(b.get("cNumBoleto") or "").strip()) if gerado else None,
            "bol_link": lk.get("l") or None,
            "bol_cb": lk.get("cb") or None,
            "bol_chave": chave_bol(t) if lk.get("l") else None,
            "sinc": sinc,
            "atualizado_em": agora_iso(),
        })

    log("Gravando no Supabase…")
    sb.gravar("omie_titulos", linhas)
    sb.apagar("omie_titulos", f"sinc=neq.{sinc}")  # títulos que saíram do Omie ou da janela de datas
    nf_linhas = [{"nf": nf, "cod_verif": v["cv"], "im": v.get("im")} for nf, v in nfse_novas.items()]
    sb.gravar("nfse", nf_linhas)
    sb.gravar_meta({"omie_sincronizado": agora_iso(), "nfse_ate": cache["nfse_ate"], "nfse_desde": cache["nfse_desde"]})

    abertos = [x for x in linhas if not fechado(x["status"])]
    resumo = [
        "### Sincronização Omie → Supabase",
        f"- Títulos desde {desde}: **{len(linhas)}** (em aberto: {len(abertos)})",
        f"- Em aberto com boleto gerado: {sum(1 for x in abertos if x['bol_num'] is not None)} | com link de 2ª via: {sum(1 for x in abertos if x['bol_link'])}",
        f"- Códigos de NFS-e gravados nesta rodada: {len(nf_linhas)}",
        f"- Tempo: {int(time.monotonic() - t0)} s",
    ]
    log("\n".join(resumo))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n".join(resumo) + "\n")


if __name__ == "__main__":
    main()
