# Central de Notas e Boletos · Payfy

Painel: https://payfy-financeiro.github.io/controle-nf-aportes/

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

## Acessos

| Senha | Usuário no Supabase | O que pode |
|---|---|---|
| Financeiro | `financeiro@central.payfy.io` | ver tudo, "Atualizar dados", "Editar" |
| CS | `cs@central.payfy.io` | só consultar (o banco recusa qualquer gravação) |
| Robô | `robo@central.payfy.io` | gravar só os dados do Omie |

As regras de acesso ficam no próprio banco (Row Level Security). Sem login, ninguém vê nada.

## Robô do Omie

- Arquivo: `.github/workflows/sincronizar-omie.yml`. Roda nos dias úteis às 7h e às 13h, ou na hora pelo botão **Actions → Sincronizar Omie → Run workflow**.
- Secrets em *Settings → Secrets and variables → Actions*: `OMIE_APP_KEY`, `OMIE_APP_SECRET` e `SUPABASE_ROBO_SENHA`.
- O robô reaproveita o que já está no Supabase (links de boleto, códigos de NFS-e), então só busca o que é novo.
- O repositório é público, então os logs mostram só contagens.

## Atualizar os aportes

No painel, com a senha do financeiro, clique em **Atualizar dados** e suba o relatório de cargas e o CSV de NFS-e da prefeitura. Cargas e notas que já estão na base são ignoradas, então não duplicam.

## Arquivos antigos

`ferramentas/painel.py` e `ferramentas/omie_chave_publica.json` são da versão anterior, quando os dados ficavam criptografados dentro do `index.html`. Não são mais usados.
