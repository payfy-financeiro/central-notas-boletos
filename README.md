# Central de Notas e Boletos · Payfy

Painel: https://payfy-financeiro.github.io/central-notas-boletos/

Duas abas:

- **Aportes (Notas Fiscais):** cargas dos clientes cruzadas com as NFS-e de aporte (código 3205).
- **Serviços (Notas Fiscais e Boletos):** contas a receber do Omie (mensalidades e demais serviços), com PDF da NF e 2ª via do boleto.

## Onde ficam os dados

Tudo fica no **Supabase**, no projeto `central-notas-boletos` (região São Paulo). Este repositório guarda só a página e o robô, sem nenhum dado.

| Tabela | Conteúdo | Quem grava |
|---|---|---|
| `aportes` | aportes e NFs de aporte, mais as edições manuais | financeiro, pelo botão "Atualizar dados" ou "Editar" |
| `omie_titulos` | títulos do contas a receber do Omie, com link do boleto | robô |
| `nfse` | código de verificação das NFS-e (para o PDF na prefeitura) | robô |
| `meta` | datas da última atualização | financeiro e robô |
| `cs_pessoas` | quem tem acesso à Central e com qual perfil | financeiro, pelo botão "Acessos" |
| `carteira` | CNPJ → CSM responsável | financeiro (planilha ou troca) e CS master (troca) |
| `carteira_log` | histórico de trocas, coberturas, importações e acessos | as próprias funções do banco |

## Acessos

Login com a conta Google, só para e-mails **@payfy.io**. Entrar com o Google não basta: a pessoa precisa estar liberada na Central.

O financeiro libera, troca o perfil e desativa pelo botão **Acessos**, no topo da Central. Quem tentar entrar sem acesso aparece sozinho em "Aguardando liberação". Ao desativar alguém com carteira, a Central pede para quem os clientes vão.

| Perfil | O que vê e faz |
|---|---|
| CS | só os clientes da própria carteira e os do Suporte |
| CS master | todos os clientes; troca CSM e cria coberturas de férias |
| Financeiro | tudo; atualiza os dados, importa a carteira e gerencia os acessos |

O robô usa o usuário `robo@central.payfy.io` e grava só os dados do Omie.

As regras de acesso ficam no próprio banco (Row Level Security). Sem login, ninguém vê nada.

## Carteira do CS

Em **Carteira do CS → Baixar modelo**, o financeiro baixa a planilha com as colunas **CNPJ** e **CSM responsável**. A coluna do nome vem com lista suspensa só com quem tem acesso (mais "Suporte").

Na importação, só mudam os clientes que estão na planilha. Linhas com nome sem acesso ficam de fora e o cliente continua com quem já estava. Antes de importar, a Central mostra quantos clientes cada pessoa recebe e de quem eles saem.

## Robô do Omie

- Arquivo: `.github/workflows/sincronizar-omie.yml`. Roda nos dias úteis às 7h e às 13h, ou na hora pelo botão **Actions → Sincronizar Omie → Run workflow**.
- Secrets em *Settings → Secrets and variables → Actions*: `OMIE_APP_KEY`, `OMIE_APP_SECRET` e `SUPABASE_ROBO_SENHA`.
- O robô reaproveita o que já está no Supabase (links de boleto, códigos de NFS-e), então só busca o que é novo.
- O repositório é público, então os logs mostram só contagens.

## Atualizar os aportes

No painel, com o acesso de financeiro, clique em **Atualizar dados** e suba o relatório de cargas e o CSV de NFS-e da prefeitura. Cargas e notas que já estão na base são ignoradas, então não duplicam.

## Arquivos antigos

`ferramentas/painel.py` e `ferramentas/omie_chave_publica.json` são da versão anterior, quando os dados ficavam criptografados dentro do `index.html`. Não são mais usados.
