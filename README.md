# Monitor do programa de grants da PSF

Verifica diariamente a [página de grants da PSF](https://www.python.org/psf/grants/)
e abre uma [discussion na APyB](https://github.com/apyb/comunidade/discussions)
quando o conteúdo muda. Python 3.13, Beautiful Soup e GitHub Actions, sem serviço externo de IA.

## O que é uma mudança relevante

O monitor compara o texto e os destinos dos links dentro do artigo do programa:
anúncios, prazos, critérios, valores, instruções, tabelas e demais seções. Ignora
menus, rodapé, scripts, sumário, espaços repetidos e marcação visual como negrito.
Links relativos são convertidos em absolutos.

Qualquer alteração nesse conteúdo é notificada, inclusive pequenas correções de
texto. Não há classificação semântica: essa escolha evita perder mudanças curtas
que importam, como uma data ou a palavra “not”. Páginas referenciadas pelos links
não são monitoradas.

A primeira execução salva a referência sem publicar. Depois, cada mudança gera
uma discussion em português com um diff do conteúdo original em inglês e o link
da PSF. Diffs muito grandes são truncados na discussion; os snapshots completos
ficam no histórico da branch `monitor-state`.

## Ativação

1. Publique este código na branch padrão do repositório e habilite GitHub Actions.
2. Em **Settings → Secrets and variables → Actions**, crie o secret
   **`DISCUSSIONS_TOKEN`**, com um token autorizado a ler e criar discussions em
   `apyb/comunidade`. Uma opção é um PAT fine-grained com a APyB como resource owner,
   acesso a `comunidade` e permissão **Discussions: Read and write**, aprovado pela
   organização quando necessário. O usuário do token também precisa poder publicar
   na categoria escolhida. Um PAT clássico com `public_repo` também é suportado
   para esse repositório público, conforme a
   [documentação da API de Discussions](https://docs.github.com/en/graphql/guides/using-the-graphql-api-for-discussions).
3. Garanta que o `GITHUB_TOKEN` do workflow possa criar e atualizar a branch
   `monitor-state` neste repositório. O workflow solicita `contents: write`;
   regras da organização ou rulesets não podem bloquear essa escrita.
4. Execute **Actions → Verificar grants da PSF → Run workflow** na branch padrão
   para criar a referência inicial. Confira o resultado e a branch `monitor-state`.

O `GITHUB_TOKEN` automático é limitado ao repositório da automação; por isso a
publicação em `apyb/comunidade` usa um secret separado. Não coloque tokens no código.

Variáveis opcionais em **Actions → Variables**:

| Variável | Padrão | Uso |
| --- | --- | --- |
| `DISCUSSIONS_REPOSITORY` | `apyb/comunidade` | Destino no formato `owner/repo` |
| `DISCUSSIONS_CATEGORY` | `Geral` | Nome exato de uma categoria existente |

O ID da categoria é resolvido pela API. Para testar a publicação, use um repositório
de teste com Discussions habilitado e um token com acesso a ele. Crie a referência
inicial; depois edite uma frase em `content` no snapshot, atualizando também seu
`hash` SHA-256 (UTF-8) e mantendo `revision`. A próxima execução detectará a diferença.

## Agendamento e persistência

O workflow roda diariamente às **12:17 UTC (09:17 em Brasília)** e também aceita
execução manual. O GitHub pode atrasar execuções agendadas e desabilita schedules
em repositórios públicos após 60 dias sem atividade; acompanhe a aba Actions e
reative o workflow se necessário. Veja as
[regras de agendamento do GitHub](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

Os snapshots são commits na branch `monitor-state`, criada automaticamente.
Não dependem de cache ou da retenção de artifacts. Execuções são serializadas para
evitar disputas pelo estado. A branch padrão guarda apenas código e documentação.

O snapshot só avança depois de uma publicação bem-sucedida, ou da confirmação de
que a mesma mudança já tem uma discussion. Um marcador determinístico no corpo
permite recuperar uma execução que publicou, mas falhou antes de salvar o estado.
A consulta percorre todas as discussions do destino sem depender do índice de busca.
Não remova esses marcadores. Se a página voltar a um conteúdo antigo depois de
outras mudanças, isso conta como um novo evento.

Falhas HTTP, timeouts, erros da API, snapshots corrompidos ou conteúdo inesperado
fazem a execução falhar sem substituir a referência. Confira o log e reexecute
depois de resolver a causa; habilite notificações de falhas do Actions no GitHub.
Se a PSF mudar o layout, ajuste `extract_content` e revise o diff antes de retomar.
Não apague a branch de estado: isso reiniciaria a referência e perderia a comparação
pendente. Para trocar o destino de publicação, conclua execuções pendentes primeiro.

## Desenvolvimento local

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python monitor.py --dry-run
```

`--dry-run` não publica e não grava arquivos. Sem snapshot, apenas valida a página.
Para visualizar um diff, use uma referência já existente:

```bash
git show origin/monitor-state:snapshot.json > /tmp/psf-snapshot.json
python monitor.py --dry-run --state /tmp/psf-snapshot.json
```

`--html caminho/pagina.html` permite usar um HTML local. Uma execução real exige
`DISCUSSIONS_TOKEN` no ambiente e usa `.local-state/snapshot.json` por padrão.
Os testes usam HTML sintético e mocks de rede, cobrindo comparação, links, tabelas,
falhas de publicação, deduplicação, paginação e persistência; não criam discussions.
