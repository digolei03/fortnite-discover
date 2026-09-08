-- Q1: colocacao causa trafego, ou trafego causa colocacao?
--
-- Esta e a pergunta que decide se qualquer leitura de rank tem valor operacional.
-- Uma correlacao alta entre posicao e volume no mesmo dia nao diz nada: pode ser
-- so a row do Discover despejando jogadores na ilha. A unica forma de separar e
-- olhar a ORDEM NO TEMPO.
--
--   forward  = variacao de trafego HOJE prevendo variacao de posicao AMANHA
--              -> trafego empurra a colocacao
--   backward = variacao de posicao HOJE prevendo variacao de trafego AMANHA
--              -> a colocacao e que traz o trafego (causalidade reversa)
--
-- Se |backward| >> |forward|, otimizar metricas para "subir no rank" e ilusao:
-- o rank e a causa, nao o efeito.

WITH lead1 AS (
    SELECT
        island_code,
        date,
        main_row,
        on_editorial,
        improve_position,
        d_ccu,
        d_unique_players,
        d_favorites,
        LEAD(improve_position) OVER w AS improve_position_next,
        LEAD(d_ccu)            OVER w AS d_ccu_next,
        LEAD(d_unique_players) OVER w AS d_unique_players_next,
        LEAD(d_favorites)      OVER w AS d_favorites_next
    FROM panel
    WINDOW w AS (PARTITION BY island_code ORDER BY date)
),
filtered AS (
    -- so rows de genero: "Top Rated"/"Popular"/"Sponsored" seguem outra logica
    SELECT * FROM lead1
    WHERE main_row IS NOT NULL
      AND on_editorial = FALSE  -- exclui dias com Epic's Picks/Sponsored
)
SELECT
    main_row                                                   AS row_name,
    COUNT(*)                                                   AS n,
    ROUND(CORR(d_ccu, improve_position_next), 3)               AS fwd_ccu,
    ROUND(CORR(improve_position, d_ccu_next), 3)               AS bwd_ccu,
    ROUND(CORR(d_unique_players, improve_position_next), 3)    AS fwd_players,
    ROUND(CORR(improve_position, d_unique_players_next), 3)    AS bwd_players,
    ROUND(CORR(d_favorites, improve_position_next), 3)         AS fwd_favorites,
    ROUND(CORR(improve_position, d_favorites_next), 3)         AS bwd_favorites,
    -- Com n grande, correlacoes minusculas viram "significantes" sem serem
    -- relevantes. Só declaramos direcao quando ha magnitude real (|r| >= 0.15)
    -- E uma diferenca clara entre os dois sentidos. Caso contrario: inconclusivo.
    CASE
        WHEN GREATEST(ABS(CORR(d_ccu, improve_position_next)),
                      ABS(CORR(improve_position, d_ccu_next))) < 0.15
             THEN 'inconclusivo (ambos ~ruido)'
        WHEN ABS(CORR(improve_position, d_ccu_next))
             > ABS(CORR(d_ccu, improve_position_next)) * 1.5
             THEN 'colocacao -> trafego'
        WHEN ABS(CORR(d_ccu, improve_position_next))
             > ABS(CORR(improve_position, d_ccu_next)) * 1.5
             THEN 'trafego -> colocacao'
        ELSE 'ambiguo'
    END                                                        AS veredito
FROM filtered
GROUP BY 1
HAVING COUNT(*) >= 200
ORDER BY n DESC;
