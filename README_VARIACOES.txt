JOHNSON BOT - SISTEMA DE VARIAÇÕES

O que foi adicionado:
- Quando um produto tiver o campo "variacoes", o cliente clica no produto e escolhe uma opção por botão.
- Cada opção pode ter nome, descrição, valor, mais_vendido e deliverables.
- O pedido é criado com o preço e nome da variação escolhida.
- O catálogo mostra "A partir de R$..." quando o produto tem variações.

Produtos já convertidos no data_single.json:
- Esdeath
- Kokushibo
- Atomic
- Gilgamesh

Os produtos duplicados desses sets foram arquivados para não aparecerem repetidos no catálogo.
O histórico de pedidos foi mantido.

Exemplo de campo para usar em outros produtos:
"variacoes": [
  {"nome": "⚔️ Normal", "descricao": "Versão básica.", "valor": 3.99},
  {"nome": "🔥 + F Move", "descricao": "Inclui F Move.", "valor": 7.99},
  {"nome": "👑 + F + Título", "descricao": "Versão completa.", "valor": 11.99, "mais_vendido": true}
]

IMPORTANTE:
- Coloque seu DISCORD_TOKEN no Railway/.env, não dentro do bot.py.
- Faça backup do seu data_single.json antigo antes de trocar.
