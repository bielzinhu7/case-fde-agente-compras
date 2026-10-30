import { useEffect, useRef, useState } from "react";

const SUGESTOES = [
  "Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada.",
  "Preciso de 4 smartphones até US$ 300 cada.",
  "Preciso de 3 laptops até US$ 2.000 cada e 2 tratores.",
];
const dolar = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "USD" });
const hora = () => new Date().toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });

// Respostas sem JSON vêm do nginx, não do backend: backend fora do ar (502) ou pedido cortado (504).
const ERRO_SEM_DETALHE = {
  502: "Não consegui falar com o backend. Ele está no ar?",
  504: "O pedido demorou demais e foi interrompido. Tente de novo.",
};

async function enviarPedido(pedido) {
  let resp;
  try {
    resp = await fetch("/api/requisicoes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pedido }),
    });
  } catch {
    return { erro: "Não consegui falar com o servidor. Confira se o projeto está rodando." };
  }
  const corpo = await resp.json().catch(() => null);
  if (!resp.ok) {
    if (typeof corpo?.detail === "string") return { erro: corpo.detail };
    return { erro: ERRO_SEM_DETALHE[resp.status] ?? `O backend respondeu HTTP ${resp.status}.` };
  }
  return { requisicao: corpo };
}

export default function App() {
  const [mensagens, setMensagens] = useState([]);
  const [texto, setTexto] = useState("");
  const [enviando, setEnviando] = useState(false);
  const fim = useRef(null);
  const campo = useRef(null);

  // Corpo com chaves de propósito: o scrollIntoView() dos navegadores novos devolve uma Promise,
  // e o React trataria esse retorno como função de limpeza do efeito (tela branca).
  useEffect(() => {
    fim.current?.scrollIntoView({ behavior: "smooth" });
  }, [mensagens, enviando]);

  // O campo cresce com o texto até 140px; só a partir daí aparece a barra de rolagem.
  useEffect(() => {
    const el = campo.current;
    if (!el) return;
    el.style.height = "auto";
    const borda = el.offsetHeight - el.clientHeight;
    el.style.height = `${Math.min(el.scrollHeight + borda, 140)}px`;
    el.style.overflowY = el.scrollHeight + borda > 140 ? "auto" : "hidden";
  }, [texto]);

  async function enviar(pedido) {
    pedido = pedido.trim();
    if (!pedido || enviando) return;
    setTexto("");
    setEnviando(true);
    setMensagens((m) => [...m, { autor: "pessoa", pedido, hora: hora() }]);
    const resposta = await enviarPedido(pedido);
    setMensagens((m) => [...m, { autor: "agente", hora: hora(), ...resposta }]);
    setEnviando(false);
    campo.current?.focus();
  }

  return (
    <div className="app">
      <header className="topo">
        <Avatar />
        <div>
          <h1>Agente de compras</h1>
          <p className="status">
            <span className="ponto" /> online · consulta o catálogo e monta a requisição
          </p>
        </div>
      </header>

      <main className="conversa" aria-live="polite">
        <div className="trilho">
          {mensagens.length === 0 && <BoasVindas aoEscolher={enviar} />}
          {mensagens.map((m, i) =>
            m.autor === "pessoa" ? (
              <div key={i} className="linha pessoa">
                <div className="balao pessoa">
                  {m.pedido}
                  <span className="hora">{m.hora}</span>
                </div>
              </div>
            ) : (
              <div key={i} className="linha agente">
                <Avatar pequeno />
                <Resposta {...m} />
              </div>
            ),
          )}
          {enviando && (
            <div className="linha agente">
              <Avatar pequeno />
              <div className="balao agente digitando" aria-label="O agente está consultando o catálogo">
                <span />
                <span />
                <span />
                <em>consultando o catálogo…</em>
              </div>
            </div>
          )}
          <div ref={fim} />
        </div>
      </main>

      <footer className="rodape">
        <form
          className="compositor"
          onSubmit={(e) => {
            e.preventDefault();
            enviar(texto);
          }}
        >
          <textarea
            ref={campo}
            value={texto}
            onChange={(e) => setTexto(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                enviar(texto);
              }
            }}
            placeholder="Escreva o que você precisa comprar…"
            aria-label="Pedido de compra"
            rows={1}
            disabled={enviando}
          />
          <button type="submit" disabled={enviando || !texto.trim()} aria-label="Enviar pedido">
            <IconeEnviar />
          </button>
        </form>
        <p className="dica">Enter envia · Shift+Enter quebra a linha · cada mensagem é um pedido novo</p>
      </footer>
    </div>
  );
}

function BoasVindas({ aoEscolher }) {
  return (
    <div className="boas-vindas">
      <Avatar grande />
      <h2>Olá! O que você precisa comprar?</h2>
      <p>Diga o produto, a quantidade e, se quiser, o preço máximo por unidade.</p>
      <div className="sugestoes">
        {SUGESTOES.map((s) => (
          <button key={s} onClick={() => aoEscolher(s)}>
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}

function Resposta({ erro, requisicao, hora }) {
  if (erro) {
    return (
      <div className="balao agente erro">
        <strong>Não deu certo.</strong> {erro}
        <span className="hora">{hora}</span>
      </div>
    );
  }

  const { numero, status, mensagem, itens, faltou = [], total, ferramentas } = requisicao;
  return (
    <div className="balao agente">
      <p className="texto">{mensagem}</p>

      {numero ? (
        <div className="cartao">
          <div className="cartao-topo">
            <span>Requisição nº {numero}</span>
            <span className={`selo ${status}`}>{status === "parcial" ? "parcial" : "criada"}</span>
          </div>
          <ul className="itens">
            {itens.map((it) => (
              <li key={it.produto_id}>
                <div>
                  <div className="produto">{it.titulo}</div>
                  <div className="conta">
                    {it.quantidade} × {dolar.format(it.preco_unitario)}
                  </div>
                </div>
                <div className="valor">{dolar.format(it.subtotal)}</div>
              </li>
            ))}
          </ul>
          <div className="total">
            <span>Total</span>
            <span>{dolar.format(total)}</span>
          </div>
        </div>
      ) : (
        <div className="selo nao_atendida">nenhuma requisição criada</div>
      )}

      {faltou.length > 0 && (
        <div className="faltou">
          <div className="faltou-titulo">Ficou de fora</div>
          <ul>
            {faltou.map((f, i) => (
              <li key={i}>
                <strong>{f.descricao}</strong>: {f.motivo}
              </li>
            ))}
          </ul>
        </div>
      )}

      {ferramentas.length > 0 && <Ferramentas lista={ferramentas} />}
      <span className="hora">{hora}</span>
    </div>
  );
}

function Ferramentas({ lista }) {
  const resumo = (f) => {
    if (f.ferramenta === "responder") {
      const n = f.argumentos?.itens?.length ?? 0;
      return n === 1 ? "1 item" : `${n} itens`;
    }
    return Object.values(f.argumentos ?? {}).join(", ");
  };
  return (
    <details className="ferramentas">
      <summary>Ferramentas chamadas ({lista.length})</summary>
      <ol>
        {lista.map((f, i) => (
          <li key={i} className={f.erro ? "falhou" : ""}>
            <code>
              {f.ferramenta}({resumo(f)})
            </code>
            {f.erro && <div className="detalhe">{f.ferramenta === "responder" ? "recusada: " : ""}{f.detalhe}</div>}
          </li>
        ))}
      </ol>
    </details>
  );
}

function Avatar({ pequeno, grande }) {
  return (
    <div className={`avatar ${pequeno ? "pequeno" : ""} ${grande ? "grande" : ""}`} aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
        <circle cx="9" cy="20" r="1.4" />
        <circle cx="17" cy="20" r="1.4" />
        <path d="M2.5 3.5h2.6l2.3 11.2h11l2.1-8H6.2" strokeLinejoin="round" />
      </svg>
    </div>
  );
}

function IconeEnviar() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true">
      <path d="M3.4 20.4 21 12 3.4 3.6l-.02 6.53L15 12 3.38 13.87z" />
    </svg>
  );
}
