"""
Servidor MCP para listar os processos de 1º grau da Justiça do Trabalho (TRT1 a TRT24)
ajuizados num dia ou período, pela API Pública do DataJud, do Conselho Nacional de Justiça
(CNJ). Padrão: ajuizados hoje no TRT19 (Alagoas). Também detalha números específicos.

O tribunal é identificado automaticamente pelo número CNJ do processo
(NNNNNNN-DD.AAAA.J.TR.OOOO): J = 5 é a Justiça do Trabalho e TR é a região
(19 = TRT da 19ª Região, Alagoas).

Modos de execução:
  python server.py            -> servidor HTTP (Streamable HTTP) em /mcp, para hospedar
  python server.py --stdio    -> servidor local via stdio
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

# ---------------------------------------------------------------------------
# Configuração (por variáveis de ambiente)
# ---------------------------------------------------------------------------

DATAJUD_BASE = "https://api-publica.datajud.cnj.jus.br"

# Chave pública divulgada pelo CNJ em https://datajud-wiki.cnj.jus.br/api-publica/acesso/
# O CNJ pode trocá-la a qualquer momento: nesse caso, defina DATAJUD_API_KEY com a nova.
CHAVE_PUBLICA_CNJ = "cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw=="
DATAJUD_API_KEY = os.environ.get("DATAJUD_API_KEY", CHAVE_PUBLICA_CNJ).strip()

# Token opcional para proteger o seu servidor. Se definido, toda chamada precisa
# enviar "Authorization: Bearer <token>" ou "?key=<token>" na URL.
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()

PORT = int(os.environ.get("PORT", "8000"))
TIMEOUT_SEGUNDOS = float(os.environ.get("DATAJUD_TIMEOUT", "30"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("trt-datajud")

# ---------------------------------------------------------------------------
# Número CNJ
# ---------------------------------------------------------------------------

GRAUS = {
    "G1": "1º grau (Vara do Trabalho)",
    "G2": "2º grau (Tribunal Regional)",
    "SUP": "Tribunal Superior (TST)",
    "TR": "Turma Recursal",
    "JE": "Juizado Especial",
}


class NumeroInvalido(ValueError):
    pass


def analisar_numero(numero: str) -> dict[str, str]:
    """Valida o número CNJ (Resolução CNJ 65/2008) e devolve suas partes."""
    digitos = re.sub(r"\D", "", numero or "")
    if len(digitos) != 20:
        raise NumeroInvalido(
            f"O número '{numero}' tem {len(digitos)} dígitos; o número CNJ tem 20 "
            "(formato NNNNNNN-DD.AAAA.J.TR.OOOO, ex.: 0000123-05.2024.5.19.0001)."
        )

    sequencial, dv, ano, ramo, regiao, origem = (
        digitos[0:7], digitos[7:9], digitos[9:13], digitos[13], digitos[14:16], digitos[16:20],
    )

    # Dígito verificador: módulo 97 (ISO 7064)
    if int(sequencial + ano + ramo + regiao + origem + dv) % 97 != 1:
        esperado = 98 - (int(sequencial + ano + ramo + regiao + origem) * 100) % 97
        raise NumeroInvalido(
            f"O dígito verificador de '{numero}' não confere (deveria ser {esperado:02d}). "
            "Confira se o número foi digitado corretamente."
        )

    if ramo != "5":
        ramos = {"1": "STF", "2": "CNJ", "3": "STJ", "4": "Justiça Federal", "6": "Justiça Eleitoral",
                 "7": "Justiça Militar da União", "8": "Justiça Estadual", "9": "Justiça Militar Estadual"}
        raise NumeroInvalido(
            f"Este número é da {ramos.get(ramo, 'outro ramo da Justiça')} (J = {ramo}), "
            "não da Justiça do Trabalho (J = 5). Este servidor consulta apenas TRTs e TST."
        )

    tr = int(regiao)
    if tr == 0:
        alias, tribunal = "api_publica_tst", "TST"
    elif 1 <= tr <= 24:
        alias, tribunal = f"api_publica_trt{tr}", f"TRT{tr}"
    else:
        raise NumeroInvalido(f"Região '{regiao}' não existe na Justiça do Trabalho (vai de 01 a 24).")

    return {
        "digitos": digitos,
        "formatado": f"{sequencial}-{dv}.{ano}.{ramo}.{regiao}.{origem}",
        "alias": alias,
        "tribunal": tribunal,
    }


# ---------------------------------------------------------------------------
# DataJud
# ---------------------------------------------------------------------------


class RequisicaoInvalida(Exception):
    """O DataJud recusou a consulta (HTTP 400): usada para tentar outro formato de busca."""


async def post_datajud(cliente: httpx.AsyncClient, alias: str, corpo: dict[str, Any]) -> dict[str, Any]:
    url = f"{DATAJUD_BASE}/{alias}/_search"
    cabecalhos = {"Authorization": f"APIKey {DATAJUD_API_KEY}", "Content-Type": "application/json"}
    try:
        resposta = await cliente.post(url, json=corpo, headers=cabecalhos)
    except httpx.TimeoutException:
        raise ToolError("O DataJud demorou demais para responder. Tente novamente em instantes.")
    except httpx.HTTPError as erro:
        raise ToolError(f"Não foi possível conectar ao DataJud: {erro.__class__.__name__}.")

    if resposta.status_code in (401, 403):
        raise ToolError(
            "O DataJud recusou a chave de acesso (HTTP "
            f"{resposta.status_code}). O CNJ pode ter trocado a chave pública: pegue a atual em "
            "https://datajud-wiki.cnj.jus.br/api-publica/acesso/ e defina-a em DATAJUD_API_KEY."
        )
    if resposta.status_code == 429:
        raise ToolError("O DataJud limitou as requisições (HTTP 429). Aguarde um pouco e tente de novo.")
    if resposta.status_code == 400:
        raise RequisicaoInvalida(resposta.text[:300])
    if resposta.status_code >= 400:
        raise ToolError(f"O DataJud respondeu com erro HTTP {resposta.status_code}.")

    try:
        return resposta.json()
    except json.JSONDecodeError:
        raise ToolError("O DataJud devolveu uma resposta que não é JSON.")


async def buscar_no_datajud(alias: str, numero_digitos: str) -> list[dict[str, Any]]:
    corpo = {"size": 20, "query": {"match": {"numeroProcesso": numero_digitos}}}
    async with httpx.AsyncClient(timeout=TIMEOUT_SEGUNDOS) as cliente:
        try:
            dados = await post_datajud(cliente, alias, corpo)
        except RequisicaoInvalida:
            raise ToolError("O DataJud recusou a consulta (HTTP 400).")
    return [h.get("_source", {}) for h in dados.get("hits", {}).get("hits", [])]


def formatar_data(valor: Any, so_data: bool = False) -> str | None:
    """Aceita '20180529000000', '20180529' ou ISO ('2023-02-10T10:31:02.000Z')."""
    if not valor:
        return None
    texto = str(valor).strip()
    try:
        if texto.isdigit() and len(texto) == 14:
            dt = datetime.strptime(texto, "%Y%m%d%H%M%S")
        elif texto.isdigit() and len(texto) == 8:
            dt = datetime.strptime(texto, "%Y%m%d")
        else:
            dt = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        return texto
    if so_data or (dt.hour, dt.minute, dt.second) == (0, 0, 0):
        return dt.strftime("%d/%m/%Y")
    return dt.strftime("%d/%m/%Y %H:%M")


def _ordem_data(valor: Any) -> str:
    """Normaliza datas em formatos diferentes para ordenar (AAAAMMDDhhmmss)."""
    return re.sub(r"\D", "", str(valor or "")).ljust(14, "0")[:14]


def _formatar_movimento(mov: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {"data": formatar_data(mov.get("dataHora")), "movimento": mov.get("nome")}
    complementos = [
        ": ".join(str(p) for p in (c.get("nome"), c.get("descricao")) if p)
        for c in (mov.get("complementosTabelados") or [])
    ]
    complementos = [c for c in complementos if c]
    if complementos:
        item["complementos"] = "; ".join(complementos)
    return item


def resumir_primeiro_grau(fonte: dict[str, Any], max_movimentos: int) -> dict[str, Any]:
    resumo: dict[str, Any] = {
        "data_ajuizamento": formatar_data(fonte.get("dataAjuizamento"), so_data=True),
        "vara": (fonte.get("orgaoJulgador") or {}).get("nome"),
        "classe": (fonte.get("classe") or {}).get("nome"),
        "assuntos": [a.get("nome") for a in (fonte.get("assuntos") or []) if isinstance(a, dict) and a.get("nome")],
        "ultima_atualizacao_datajud": formatar_data(fonte.get("dataHoraUltimaAtualizacao")),
    }
    if fonte.get("nivelSigilo"):
        resumo["sigilo"] = f"sigiloso (nível {fonte['nivelSigilo']})"
    if max_movimentos > 0:
        movimentos = sorted(fonte.get("movimentos") or [], key=lambda m: _ordem_data(m.get("dataHora")), reverse=True)
        resumo["total_movimentos"] = len(movimentos)
        resumo["movimentos"] = [_formatar_movimento(m) for m in movimentos[:max_movimentos]]
    return resumo


NAO_ENCONTRADO = (
    "Processo não encontrado no DataJud. Possíveis motivos: segredo de justiça, ainda não enviado "
    "pelo tribunal ao DataJud (pode haver alguns dias de atraso) ou número incorreto."
)


async def consultar_primeiro_grau(numero: str, max_movimentos: int) -> dict[str, Any]:
    """Consulta um número e devolve só o registro de 1º grau (G1)."""
    try:
        info = analisar_numero(numero)
    except NumeroInvalido as erro:
        return {"numero_processo": numero, "encontrado": False, "motivo": str(erro)}

    base = {"numero_processo": info["formatado"], "tribunal": info["tribunal"]}
    if info["alias"] == "api_publica_tst":
        return {**base, "encontrado": False, "motivo": "Processo originário do TST: não tem 1º grau."}

    try:
        fontes = await buscar_no_datajud(info["alias"], info["digitos"])
    except ToolError as erro:
        return {**base, "encontrado": False, "motivo": str(erro)}

    primeiro_grau = [f for f in fontes if f.get("grau") == "G1"]
    if not primeiro_grau:
        if fontes:
            graus = sorted({GRAUS.get(f.get("grau"), f.get("grau") or "?") for f in fontes})
            motivo = f"Está no DataJud, mas sem registro de 1º grau (aparece em: {', '.join(graus)})."
        else:
            motivo = NAO_ENCONTRADO
        return {**base, "encontrado": False, "motivo": motivo}

    # Se houver mais de um registro de 1º grau, usa o atualizado mais recentemente
    fonte = max(primeiro_grau, key=lambda f: _ordem_data(f.get("dataHoraUltimaAtualizacao")))
    return {**base, "encontrado": True, **resumir_primeiro_grau(fonte, max_movimentos)}


# ---------------------------------------------------------------------------
# Listagem por data de ajuizamento
# ---------------------------------------------------------------------------

FUSO_BRASILIA = timezone(timedelta(hours=-3))  # Alagoas e Brasília: UTC-3, sem horário de verão
TAMANHO_PAGINA = int(os.environ.get("DATAJUD_TAMANHO_PAGINA", "500"))
MAX_REGISTROS_BUSCADOS = int(os.environ.get("DATAJUD_MAX_REGISTROS", "5000"))
MAX_DIAS_PERIODO = 31
CAMPOS_LISTAGEM = ["numeroProcesso", "dataAjuizamento", "orgaoJulgador", "classe", "grau", "@timestamp"]

# Combinação (formato do filtro de data, filtrar campos) que o DataJud aceitou da última vez
_estrategia_ok: tuple[str, bool] | None = None


def hoje() -> date:
    return datetime.now(FUSO_BRASILIA).date()


def interpretar_data(texto: str | None, padrao: date) -> date:
    if texto is None or not str(texto).strip():
        return padrao
    t = str(texto).strip().lower()
    if t in ("hoje", "today"):
        return hoje()
    if t in ("ontem", "yesterday"):
        return hoje() - timedelta(days=1)
    for formato in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(t, formato).date()
        except ValueError:
            pass
    raise ToolError(f"Não entendi a data '{texto}'. Use 'hoje', 'ontem', dd/mm/aaaa ou aaaa-mm-dd.")


def data_do_registro(valor: Any) -> date | None:
    texto = re.sub(r"\D", "", str(valor or ""))[:8]
    try:
        return datetime.strptime(texto, "%Y%m%d").date()
    except ValueError:
        return None


def filtro_periodo(inicio: date, fim: date, modo: str) -> dict[str, Any]:
    """Filtro de dataAjuizamento entre inicio e fim (inclusive).

    modo "data": o campo é do tipo data no Elasticsearch (comparação por data).
    modo "texto": o campo é texto; compara os dois formatos usados pelos tribunais
    ("20261005000000" e "2026-10-05T00:00:00.000Z") em ordem alfabética.
    """
    a, b = inicio.isoformat(), (fim + timedelta(days=1)).isoformat()
    if modo == "data":
        return {"range": {"dataAjuizamento": {"gte": a, "lt": b, "format": "yyyy-MM-dd"}}}
    return {"bool": {"minimum_should_match": 1, "should": [
        {"range": {"dataAjuizamento": {"gte": a, "lt": b}}},
        {"range": {"dataAjuizamento": {"gte": a.replace("-", ""), "lt": b.replace("-", "")}}},
    ]}}


def _estrategias() -> list[tuple[str, bool]]:
    todas = [("data", True), ("data", False), ("texto", True), ("texto", False)]
    if _estrategia_ok in todas:
        todas.remove(_estrategia_ok)
        todas.insert(0, _estrategia_ok)
    return todas


async def _paginar(cliente, alias, inicio, fim, modo, filtrar_campos) -> tuple[list[dict[str, Any]], bool]:
    registros: list[dict[str, Any]] = []
    depois_de = None
    while True:
        corpo: dict[str, Any] = {
            "size": TAMANHO_PAGINA,
            "query": {"bool": {"filter": [{"match": {"grau": "G1"}}, filtro_periodo(inicio, fim, modo)]}},
            "sort": [{"@timestamp": {"order": "asc"}}],
        }
        if filtrar_campos:
            corpo["_source"] = CAMPOS_LISTAGEM
        if depois_de:
            corpo["search_after"] = depois_de
        hits = (await post_datajud(cliente, alias, corpo)).get("hits", {}).get("hits", [])
        registros.extend(h.get("_source", {}) for h in hits)
        depois_de = hits[-1].get("sort") if hits else None
        if not hits or not depois_de or len(hits) < TAMANHO_PAGINA:
            return registros, False
        if len(registros) >= MAX_REGISTROS_BUSCADOS:
            return registros, True


async def buscar_por_periodo(
    alias: str, inicio: date, fim: date
) -> tuple[list[dict[str, Any]], bool, tuple[str, bool]]:
    """Busca registros de 1º grau ajuizados no período. Devolve (registros, truncado, estratégia)."""
    global _estrategia_ok
    ultimo_erro = ""
    async with httpx.AsyncClient(timeout=TIMEOUT_SEGUNDOS) as cliente:
        for modo, filtrar_campos in _estrategias():
            try:
                registros, truncado = await _paginar(cliente, alias, inicio, fim, modo, filtrar_campos)
            except RequisicaoInvalida as erro:
                ultimo_erro = str(erro)
                log.info("DataJud recusou a estratégia %s/%s: %s", modo, filtrar_campos, ultimo_erro[:120])
                continue
            _estrategia_ok = (modo, filtrar_campos)
            return registros, truncado, _estrategia_ok
    raise ToolError(f"O DataJud não aceitou a busca por data de ajuizamento. Detalhe: {ultimo_erro[:200]}")


async def data_mais_recente(alias: str, estrategia: tuple[str, bool]) -> str | None:
    """Data de ajuizamento mais recente de 1º grau disponível no DataJud (últimos 120 dias)."""
    modo, filtrar_campos = estrategia
    corpo: dict[str, Any] = {
        "size": 1,
        "query": {"bool": {"filter": [
            {"match": {"grau": "G1"}},
            filtro_periodo(hoje() - timedelta(days=120), hoje(), modo),
        ]}},
        "sort": [{"dataAjuizamento": {"order": "desc"}}],
    }
    if filtrar_campos:
        corpo["_source"] = ["dataAjuizamento"]
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SEGUNDOS) as cliente:
            hits = (await post_datajud(cliente, alias, corpo)).get("hits", {}).get("hits", [])
    except (ToolError, RequisicaoInvalida):
        return None
    if not hits:
        return None
    d = data_do_registro(hits[0].get("_source", {}).get("dataAjuizamento"))
    return d.strftime("%d/%m/%Y") if d else None


def _sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn")


def formatar_numero(digitos: str) -> str:
    d = re.sub(r"\D", "", digitos or "")
    if len(d) != 20:
        return digitos
    return f"{d[0:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:20]}"


# ---------------------------------------------------------------------------
# Servidor MCP
# ---------------------------------------------------------------------------

MAX_PROCESSOS = 50
CONSULTAS_SIMULTANEAS = 5

mcp = MCPServer(
    name="trt-datajud",
    title="Processos de 1º grau da Justiça do Trabalho (DataJud/CNJ)",
    version="1.2.0",
    instructions=(
        "Processos de 1º grau (Varas do Trabalho) dos TRTs da 1ª à 24ª Região, pela API Pública do "
        "DataJud (CNJ). Use listar_processos_ajuizados para descobrir os números dos processos "
        "ajuizados num dia ou período (padrão: hoje, TRT19 = Alagoas). Use consultar_processos_trt "
        "para detalhar números específicos. Mostre sempre a data de ajuizamento de cada processo e "
        "avise que o DataJud tem alguns dias de atraso em relação ao PJe."
    ),
)


@mcp.tool(
    name="consultar_processos_trt",
    title="Consultar processos de 1º grau (TRT)",
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True),
)
async def consultar_processos_trt(
    numeros_processos: list[str],
    max_movimentos: int = 0,
) -> dict[str, Any]:
    """Consulta processos trabalhistas no 1º grau (Varas do Trabalho) pela API Pública do DataJud (CNJ).

    Para cada número, devolve a data de ajuizamento, a vara, a classe e os assuntos do processo
    no 1º grau. Registros de 2º grau e do TST são ignorados.

    Args:
        numeros_processos: Um ou mais números CNJ (até 50), com ou sem pontuação
            (ex.: ["0000123-05.2024.5.19.0001"]). O TRT é identificado pelo próprio número.
        max_movimentos: Quantas movimentações mais recentes do 1º grau incluir por processo
            (padrão 0 = nenhuma). Use apenas se o usuário pedir as movimentações.
    """
    numeros = [n for n in (numeros_processos or []) if str(n).strip()]
    if not numeros:
        raise ToolError("Informe ao menos um número de processo.")
    if len(numeros) > MAX_PROCESSOS:
        raise ToolError(f"Envie no máximo {MAX_PROCESSOS} números por consulta (recebi {len(numeros)}).")

    limite = asyncio.Semaphore(CONSULTAS_SIMULTANEAS)

    async def consultar(numero: str) -> dict[str, Any]:
        async with limite:
            return await consultar_primeiro_grau(numero, max_movimentos)

    processos = await asyncio.gather(*(consultar(n) for n in numeros))
    encontrados = sum(1 for p in processos if p["encontrado"])
    log.info("consulta de %d processo(s): %d encontrado(s) no 1º grau", len(processos), encontrados)

    return {
        "fonte": "API Pública do DataJud (CNJ)",
        "grau": "1º grau (Varas do Trabalho)",
        "total_consultados": len(processos),
        "encontrados": encontrados,
        "processos": processos,
    }


@mcp.tool(
    name="listar_processos_ajuizados",
    title="Listar processos ajuizados por data (1º grau)",
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True),
)
async def listar_processos_ajuizados(
    data_inicio: str = "hoje",
    data_fim: str | None = None,
    trt: int = 19,
    vara_contem: str | None = None,
    limite: int = 300,
) -> dict[str, Any]:
    """Lista os números dos processos de 1º grau ajuizados num dia ou período, com a data de
    ajuizamento, a vara e a classe de cada um. Padrão: ajuizados hoje no TRT19 (Alagoas).

    Fonte: API Pública do DataJud (CNJ). Os tribunais enviam os dados ao CNJ com atraso de alguns
    dias, então os dias mais recentes podem aparecer incompletos ou vazios.

    Args:
        data_inicio: Primeiro dia do ajuizamento: "hoje", "ontem", "dd/mm/aaaa" ou "aaaa-mm-dd".
        data_fim: Último dia (inclusive). Se omitido, consulta só o dia de data_inicio.
            O período pode ter no máximo 31 dias.
        trt: Região do TRT, de 1 a 24 (padrão 19 = Alagoas).
        vara_contem: Filtra pelo nome da vara, ex.: "Maceió" ou "Vara do Trabalho de Arapiraca".
        limite: Máximo de processos listados na resposta (padrão 300, máx. 500). O total
            encontrado é sempre informado.
    """
    if not 1 <= trt <= 24:
        raise ToolError("O TRT deve ser de 1 a 24.")
    inicio = interpretar_data(data_inicio, hoje())
    fim = interpretar_data(data_fim, inicio)
    if fim < inicio:
        inicio, fim = fim, inicio
    if (fim - inicio).days + 1 > MAX_DIAS_PERIODO:
        raise ToolError(f"O período pode ter no máximo {MAX_DIAS_PERIODO} dias. Divida em partes menores.")
    limite = max(1, min(int(limite), 500))

    alias, tribunal = f"api_publica_trt{trt}", f"TRT{trt}"
    registros, truncado, estrategia = await buscar_por_periodo(alias, inicio, fim)

    # Confere no próprio servidor: só 1º grau, só dentro do período, sem duplicados
    filtro = _sem_acento(vara_contem) if vara_contem and vara_contem.strip() else None
    por_numero: dict[str, dict[str, Any]] = {}
    for r in registros:
        d = data_do_registro(r.get("dataAjuizamento"))
        if r.get("grau") != "G1" or d is None or not inicio <= d <= fim:
            continue
        vara = (r.get("orgaoJulgador") or {}).get("nome") or ""
        if filtro and filtro not in _sem_acento(vara):
            continue
        numero = re.sub(r"\D", "", str(r.get("numeroProcesso") or ""))
        anterior = por_numero.get(numero)
        if anterior is None or str(r.get("@timestamp") or "") > str(anterior["_ts"]):
            por_numero[numero] = {
                "_ts": r.get("@timestamp") or "",
                "_data": d,
                "numero_processo": formatar_numero(numero),
                "data_ajuizamento": d.strftime("%d/%m/%Y"),
                "vara": vara or None,
                "classe": (r.get("classe") or {}).get("nome"),
            }

    processos = sorted(por_numero.values(), key=lambda p: (p["_data"], p["numero_processo"]))
    for p in processos:
        p.pop("_ts"), p.pop("_data")

    periodo = inicio.strftime("%d/%m/%Y") if inicio == fim else f"{inicio:%d/%m/%Y} a {fim:%d/%m/%Y}"
    log.info("listagem %s %s: %d processo(s)", tribunal, periodo, len(processos))

    resultado: dict[str, Any] = {
        "tribunal": tribunal,
        "grau": "1º grau (Varas do Trabalho)",
        "periodo_ajuizamento": periodo,
        "total_encontrado": len(processos),
        "processos": processos[:limite],
    }
    if vara_contem:
        resultado["filtro_vara"] = vara_contem
    if len(processos) > limite:
        resultado["observacao"] = f"Listando os {limite} primeiros de {len(processos)}. Aumente 'limite' (máx. 500) ou reduza o período."
    if truncado:
        resultado["observacao_busca"] = (
            f"A busca parou em {MAX_REGISTROS_BUSCADOS} registros do DataJud; o total real pode ser maior. "
            "Reduza o período."
        )
    if fim >= hoje() - timedelta(days=7):
        resultado["aviso"] = (
            "O DataJud recebe os dados dos tribunais com atraso de alguns dias, então processos "
            "ajuizados nos últimos dias podem ainda não aparecer."
        )
    if not processos:
        recente = await data_mais_recente(alias, estrategia)
        if recente:
            resultado["ajuizamento_mais_recente_no_datajud"] = recente
    return resultado


# ---------------------------------------------------------------------------
# HTTP: verificação de saúde + token opcional
# ---------------------------------------------------------------------------


@mcp.custom_route("/", methods=["GET"], include_in_schema=False)
async def saude(_request):
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok", "servidor": "trt-datajud", "mcp": "/mcp"})


class TokenOpcional:
    """Middleware ASGI: se MCP_AUTH_TOKEN estiver definido, exige o token em /mcp."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.token or not scope.get("path", "").startswith("/mcp"):
            return await self.app(scope, receive, send)

        cabecalhos = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        enviado = ""
        autorizacao = cabecalhos.get("authorization", "")
        if autorizacao.lower().startswith("bearer "):
            enviado = autorizacao[7:].strip()
        if not enviado:
            consulta = scope.get("query_string", b"").decode()
            achado = re.search(r"(?:^|&)key=([^&]+)", consulta)
            if achado:
                from urllib.parse import unquote

                enviado = unquote(achado.group(1))

        if enviado and hmac.compare_digest(enviado, self.token):
            return await self.app(scope, receive, send)

        corpo = json.dumps({"erro": "token inválido ou ausente"}).encode()
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(corpo)).encode())],
        })
        await send({"type": "http.response.body", "body": corpo})


def criar_app():
    app = mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")
    return TokenOpcional(app, MCP_AUTH_TOKEN)


if __name__ == "__main__":
    if "--stdio" in sys.argv or os.environ.get("MCP_TRANSPORT") == "stdio":
        mcp.run("stdio")
    else:
        import uvicorn

        if not MCP_AUTH_TOKEN:
            log.warning("MCP_AUTH_TOKEN não definido: qualquer pessoa com a URL poderá usar o servidor.")
        uvicorn.run(criar_app(), host="0.0.0.0", port=PORT, proxy_headers=True, forwarded_allow_ips="*")
