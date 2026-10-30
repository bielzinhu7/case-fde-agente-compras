import { useEffect, useRef, useState } from "react";

const EXEMPLO = "Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada.";
const dolar = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "USD" });

async function enviarPedido(pedido) {
  let resp;
  try {
    resp = await fetch("/api/requisicoes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pedido }),
    });
  } catch {
    return { erro: "Não consegui falar com o backend. Ele está no ar?" };
  }
  const corpo = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detalhe = typeof corpo.detail === "string" ? corpo.detail : `O backend respondeu HTTP ${resp.status}.`;
    return { erro: detalhe };
  }
  return { requisicao: corpo };
}

export default function App() {
  const [mensagens, setMensagens] = useState([]);
  const [texto, setTexto] = useState("");
  const [enviando, setEnviando] = useState(false);
  const fim = useRef(null);

  // Corpo com chaves de propósito: o scrollIntoView() dos navegadores novos devolve uma Promise,
  // e o React trataria esse retorno como função de limpeza do efeito (tela branca).
  useEffect(() => {
    fim.current?.scrollIntoView({ behavior: "smooth" });
  }, [mensagens, enviando]);

  async function enviar(pedido) {
    pedido = pedido.trim();
    if (!pedido || enviando) return;
    setTexto("");
    setEnviando(true);
    setMensagens((m) => [...m, { autor: "pessoa", pedido }]);
    const resposta = await enviarPedido(pedido);
    setMensagens((m) => [...m, { autor: "agente", ...resposta }]);
    setEnviando(false);
  }

  return (
    <div className="app">
      <header>
        <h1>Agente de compras</h1>
        <p>Descreva o que precisa. O agente consulta o catálogo e monta a requisição de compra.</p>
      </header>

      <main className="conversa">
        {mensagens.length === 0 && (
          <button className="exemplo" onClick={() => enviar(EXEMPLO)}>
            Experimente: “{EXEMPLO}”
          </button>
        )}
        {mensagens.map((m, i) =>
          m.autor === "pessoa" ? (
            <div key={i} className="balao pessoa">{m.pedido}</div>
          ) : (
            <Resposta key={i} {...m} />
          ),
        )}
        {enviando && <div className="balao agente pensando">Consultando o catálogo…</div>}
        <div ref={fim} />
      </main>

      <form
        className="entrada"
        onSubmit={(e) => {
          e.preventDefault();
          enviar(texto);
        }}
      >
        <textarea
          value={texto}
          onChange={(e) => setTexto(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              enviar(texto);
            }
          }}
          placeholder="Ex.: Preciso de 2 smartphones até US$ 800 cada"
          rows={2}
          disabled={enviando}
        />
        <button type="submit" disabled={enviando || !texto.trim()}>
          Enviar
        </button>
      </form>
    </div>
  );
}

function Resposta({ erro, requisicao }) {
  if (erro) return <div className="balao agente erro">{erro}</div>;

  const { numero, mensagem, itens, total, ferramentas } = requisicao;
  return (
    <div className="balao agente">
      {numero ? (
        <div className="selo criada">Requisição nº {numero} criada</div>
      ) : (
        <div className="selo nao-atendida">Pedido não atendido</div>
      )}
      <p>{mensagem}</p>

      {itens.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Item</th>
              <th>Qtd.</th>
              <th>Preço unit.</th>
              <th>Subtotal</th>
            </tr>
          </thead>
          <tbody>
            {itens.map((it) => (
              <tr key={it.produto_id}>
                <td>{it.titulo}</td>
                <td>{it.quantidade}</td>
                <td>{dolar.format(it.preco_unitario)}</td>
                <td>{dolar.format(it.subtotal)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td colSpan={3}>Total</td>
              <td>{dolar.format(total)}</td>
            </tr>
          </tfoot>
        </table>
      )}

      {ferramentas.length > 0 && (
        <details>
          <summary>Ferramentas chamadas ({ferramentas.length})</summary>
          <ol>
            {ferramentas.map((f, i) => (
              <li key={i} className={f.erro ? "falhou" : ""}>
                <code>
                  {f.ferramenta}({JSON.stringify(f.argumentos)})
                </code>
                {f.erro && " — erro"}
              </li>
            ))}
          </ol>
        </details>
      )}
    </div>
  );
}
