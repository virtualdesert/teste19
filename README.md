# Servidor MCP: processos ajuizados no 1º grau da Justiça do Trabalho (TRT19 e demais TRTs)

Conector para o Claude descobrir **quais processos foram ajuizados num dia ou período** nas Varas do
Trabalho, pela **API Pública do DataJud (CNJ)**, de graça e direto da fonte oficial. Para cada
processo, mostra o **número**, a **data de ajuizamento**, a vara e a classe.

- Padrão: processos de **1º grau** ajuizados **hoje** no **TRT19 (Alagoas)**.
- Dá para escolher outro dia, um período de até 31 dias, outro TRT (1 a 24) ou filtrar por vara.
- Registros de 2º grau e do TST são ignorados.

## Ferramentas

### `listar_processos_ajuizados` (principal)

| Parâmetro | Padrão | O que faz |
|---|---|---|
| `data_inicio` | `hoje` | Primeiro dia: `hoje`, `ontem`, `dd/mm/aaaa` ou `aaaa-mm-dd` |
| `data_fim` | igual ao início | Último dia (inclusive); período de até 31 dias |
| `trt` | 19 | Região do TRT (1 a 24) |
| `vara_contem` | (nenhum) | Filtra pelo nome da vara, ex.: `Maceió`, `Arapiraca` (ignora acentos) |
| `limite` | 300 | Máximo de processos na resposta (até 500); o total é sempre informado |

A lista vem ordenada por data de ajuizamento. Quando o período não tem nenhum processo, a resposta
informa a data de ajuizamento mais recente que já existe no DataJud para aquele TRT. Assim dá para
saber até onde os dados estão atualizados.

### `consultar_processos_trt` (detalhar números específicos)

Recebe até 50 números CNJ e devolve, do 1º grau:

- data de ajuizamento
- vara
- classe e assuntos

Com `max_movimentos`, inclui as movimentações mais recentes.

### Exemplos de pedidos ao Claude

- "Quais processos foram ajuizados hoje no TRT19?"
- "Liste os processos ajuizados ontem nas varas de Maceió"
- "Quantos processos foram ajuizados no TRT19 entre 01/09/2026 e 15/09/2026? Monte uma tabela por vara"
- "Detalhe o processo 0000123-05.2024.5.19.0001"

## Opção A: hospedar e usar no Claude (web, desktop e celular)

Os conectores personalizados do Claude acessam o servidor pela internet. Por isso, ele precisa
estar hospedado num endereço público. Abaixo, o caminho pelo Render; Railway e Fly.io funcionam
do mesmo jeito, pois todos aceitam o `Dockerfile`.

### 1. Gerar um token de acesso

Sem token, qualquer pessoa com a URL consegue usar o seu servidor. Gere um token aleatório:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 2. Publicar o servidor

1. Crie um repositório no GitHub com os arquivos desta pasta (`server.py`, `requirements.txt`,
   `Dockerfile`, `README.md`).
2. No [Render](https://render.com), clique em **New → Web Service** e conecte o repositório.
   O Render detecta o `Dockerfile`.
3. Em **Environment**, crie a variável `MCP_AUTH_TOKEN` com o token do passo 1.
4. Clique em **Deploy**. Quando terminar, abra `https://SEU-SERVICO.onrender.com/`. Deve aparecer
   `{"status": "ok", ...}`.

> O plano gratuito do Render "dorme" depois de um tempo sem uso. Nesse caso, a primeira consulta
> pode demorar perto de um minuto ou falhar por tempo esgotado; é só repetir. Um plano pago evita isso.

### 3. Adicionar ao Claude

1. Vá em **Customize → Connectors**, clique em **+** e depois em **Add custom connector**.
2. Em **URL**, use o endereço com `/mcp` no final e o token. Há duas formas:
   - **Com cabeçalho (preferível):** URL `https://SEU-SERVICO.onrender.com/mcp`. Em
     **Request headers**, adicione `Authorization` com o valor `Bearer SEU_TOKEN`.
   - **Com o token na URL:** se o diálogo não tiver a opção de cabeçalhos, use
     `https://SEU-SERVICO.onrender.com/mcp?key=SEU_TOKEN`.
3. Clique em **Add** e ative o conector na conversa.

Guia oficial: <https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp>

## Opção B: rodar no seu computador (sem hospedagem)

Se o seu aplicativo de IA aceita servidores MCP locais (via stdio), instale as dependências
e aponte para o servidor no modo `--stdio`:

```bash
pip install -r requirements.txt
```

```json
{
  "mcpServers": {
    "trt-datajud": {
      "command": "python",
      "args": ["/caminho/para/server.py", "--stdio"]
    }
  }
}
```

Para testar o modo HTTP localmente, rode `python server.py` e abra <http://localhost:8000/>.
O endpoint MCP fica em `http://localhost:8000/mcp`.

## Variáveis de ambiente

| Variável | Padrão | Uso |
|---|---|---|
| `MCP_AUTH_TOKEN` | (vazio) | Token exigido em `/mcp`. Recomendado ao hospedar. |
| `DATAJUD_API_KEY` | chave pública atual do CNJ | Troque se o CNJ mudar a chave. A atual fica em <https://datajud-wiki.cnj.jus.br/api-publica/acesso/>. |
| `PORT` | 8000 | Porta HTTP (o Render define sozinho). |
| `DATAJUD_TIMEOUT` | 30 | Tempo máximo de espera pelo DataJud, em segundos. |
| `DATAJUD_MAX_REGISTROS` | 5000 | Teto de registros lidos do DataJud numa listagem. |

## Limitações do DataJud

- Traz metadados e movimentações. Não traz o conteúdo dos documentos (petições, sentenças) e, em
  geral, nem os nomes das partes.
- **Atraso:** os tribunais enviam os dados ao CNJ periodicamente, então pode haver dias de atraso em
  relação ao PJe. Uma consulta de "hoje" pode vir vazia ou incompleta. Quando vem vazia, a resposta
  mostra a data mais recente disponível; consulte essa data ou um período anterior.
- Processos em segredo de justiça podem não aparecer.
- Os horários das movimentações são mostrados como vêm do DataJud.
- O uso está sujeito aos
  [termos de uso da API Pública do CNJ](https://formularios.cnj.jus.br/wp-content/uploads/2023/11/Termos-de-uso-api-publica-V1.2.pdf)
  e à LGPD.

Se o servidor responder que a chave foi recusada, o CNJ provavelmente trocou a chave pública.
Copie a nova da wiki do DataJud para a variável `DATAJUD_API_KEY` e reinicie o serviço.
