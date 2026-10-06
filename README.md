# Controle de NFs por Aporte · Payfy

Painel: https://payfy-financeiro.github.io/controle-nf-aportes/

## Acessos

| Senha | O que libera |
|---|---|
| Financeiro | Tudo: consulta, **Atualizar dados**, **Editar**, exportar |
| CS | Somente consulta: aportes × NFs, contas a receber, PDF da NF, 2ª via do boleto, exportar |

As senhas **não** ficam neste repositório. Os dados vão criptografados dentro do `index.html` (AES-GCM + PBKDF2). Uma chave de dados única é "embrulhada" por cada senha (linha `const KEYS=`), então trocar a senha do CS não mexe nos dados.

## Contas a receber (Omie)

- **Robô:** `.github/workflows/sincronizar-omie.yml`, roda nos dias úteis às 7h e às 13h. Também dá para rodar na hora em **Actions → Sincronizar Omie → Run workflow**.
- **O que puxa:** clientes, categorias e títulos do contas a receber com vencimento ou emissão desde 01/01/2025 (para mudar, crie a variável `OMIE_DESDE` em *Settings → Secrets and variables → Actions → Variables*). Puxa também o link da 2ª via dos boletos em aberto e, se as NFS-e forem emitidas pelo Omie, o código de verificação.
- **Onde grava:** `omie.json`, criptografado, no ramo `dados`. O robô só tem a chave **pública** (`ferramentas/omie_chave_publica.json`). A chave privada fica dentro do pacote criptografado do painel, então só quem tem uma das senhas consegue ler.
- **Secrets obrigatórios** (*Settings → Secrets and variables → Actions*): `OMIE_APP_KEY` e `OMIE_APP_SECRET`.
- **Logs:** o repositório é público, então o robô só registra contagens e nomes de campos. Nunca registra valores, clientes ou CNPJs.

## Como os títulos se ligam aos aportes

1. **CNPJ do cliente:** cada aporte mostra um selo como "Omie · 2 vencidos · 1 a vencer". Ao clicar, abre a aba de contas a receber já filtrada por aquele CNPJ.
2. **Nº da NF do título:** o botão **NF** abre o PDF na prefeitura de SP. O código de verificação vem do Omie ou das notas de aporte que já estão no painel. Sem o código, abre a tela de verificação com os dados para copiar.

## Manutenção (`ferramentas/painel.py`, requer `pip install cryptography`)

- Trocar a senha do CS (gera uma nova e imprime na tela):
  `python ferramentas/painel.py senha index.html --senha <SENHA_FINANCEIRO> --papel cs`
- Abrir a base para atualizar e depois fechar de novo:
  `python ferramentas/painel.py abrir index.html --senha <SENHA_FINANCEIRO> --saida dados.json`
  `python ferramentas/painel.py fechar index.html dados.json --senha <SENHA_FINANCEIRO>`
- **Importante:** ao gerar um `index.html` novo, preserve a linha `const KEYS=` e o campo `K` do pacote (o comando `fechar` já faz isso). Sem eles, a senha do CS para de funcionar e a aba do Omie não abre.
