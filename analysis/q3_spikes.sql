-- Q3: quanto tempo dura um destaque, e o que faz ele durar?
--
-- O padrao que motivou o projeto: BACKROOMS: BEST LEVELS apareceu em #7 do horror
-- e caiu para #227 em poucos dias - passando por #7 -> #16 -> #28 -> #40 -> #51
-- -> #99 em SEIS HORAS. Isso e a Epic dando distribuicao de teste e recolhendo
-- quando os dados de retencao voltam ruins.
--
-- Esta consulta identifica cada episodio de destaque e mede a sobrevivencia,
-- cruzando com a retencao e a taxa de favoritagem no inicio do episodio.
-- E o que permite prever se um destaque recebido hoje vai decair.

WITH marked AS (
    SELECT
        island_code, date, main_row, position, position_prev,
        retention_d1, retention_d7, favorite_rate, unique_players,
        -- entrou num destaque: melhorou 30+ posicoes de um dia para o outro
        CASE WHEN position_prev IS NOT NULL
              AND position_prev - position >= 30 THEN 1 ELSE 0 END AS is_spike
    FROM panel
    WHERE main_row IS NOT NULL
      AND on_editorial = FALSE
      AND position IS NOT NULL
),
episodes AS (
    SELECT
        m.island_code, m.main_row,
        m.date                                   AS spike_date,
        m.position_prev                          AS position_before,
        m.position                               AS position_at_spike,
        m.position_prev - m.position             AS jump,
        m.retention_d1, m.retention_d7, m.favorite_rate,
        -- posicao 7 dias depois e melhor posicao sustentada no intervalo
        (SELECT p.position FROM panel p
          WHERE p.island_code = m.island_code
            AND p.date = m.date + INTERVAL 7 DAY)             AS position_after_7d,
        (SELECT COUNT(*) FROM panel p
          WHERE p.island_code = m.island_code
            AND p.date > m.date
            AND p.date <= m.date + INTERVAL 14 DAY
            AND p.position <= m.position * 1.5)               AS days_held_14d
    FROM marked m
    WHERE m.is_spike = 1
)
SELECT
    island_code, main_row, spike_date,
    position_before, position_at_spike, jump,
    position_after_7d,
    days_held_14d,
    ROUND(retention_d1, 3)  AS d1,
    ROUND(retention_d7, 3)  AS d7,
    ROUND(favorite_rate, 4) AS fav_rate,
    CASE
        WHEN position_after_7d IS NULL                    THEN 'saiu da row'
        WHEN position_after_7d <= position_at_spike * 1.5 THEN 'sustentou'
        ELSE 'decaiu'
    END AS desfecho
FROM episodes
ORDER BY spike_date DESC, jump DESC;
