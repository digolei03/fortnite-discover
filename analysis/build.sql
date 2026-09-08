-- Views derivadas do painel.
--
-- As views de ORIGEM (islands, placements_raw, players_daily, ranks, metrics,
-- genres) sao criadas por analysis/run.py, que tolera dataset ainda inexistente
-- criando uma view vazia com o schema correto. Isso importa no dia 1 e quando
-- um coletor falha: as analises degradam em vez de estourar.

-- Taxonomia das rows do Discover.
--
-- Distincao critica: `Epic's Picks` e `Sponsored` sao colocacoes EDITORIAIS /
-- pagas. A documentacao da Epic diz que toda row e algoritmica cega EXCETO
-- Epic's Picks e By Epic. Misturar as duas coisas contamina qualquer analise:
-- uma ilha com retencao pessima pode estar no topo simplesmente porque foi
-- escolhida a mao. Toda pergunta causal roda so sobre `genre` e `algorithmic`.
CREATE OR REPLACE VIEW row_taxonomy AS
SELECT DISTINCT
    row_name,
    CASE
        WHEN row_name IN ('Epic''s Picks', 'By Epic', 'Sponsored')      THEN 'editorial'
        WHEN row_name IN ('Shooter', 'Battle Royale', 'Simulation', 'Simulation & Tycoon',
                          'Party Games', 'Roleplay', 'Deathrun', 'Horror', 'Roguelike',
                          'Strategy', 'Adventure', 'Survival', 'Sports', 'Racing',
                          'Music', 'Party & Mini Games', 'Roleplaying & Social',
                          'Deathrun & Platformer', 'Adventure & RPG', 'Sports & Racing',
                          'Music & Rhythm')                             THEN 'genre'
        WHEN row_name IN ('New')                                        THEN 'new'
        WHEN row_name IN ('Popular', 'Top Rated', 'Trending', 'Trending Variety',
                          'Most Engaging', 'People Love', 'Top 10',
                          'Great With Friends', 'Variety')              THEN 'algorithmic'
        ELSE 'contextual'   -- Homebar, "Inspired by ...", curadorias sazonais
    END AS row_kind
FROM placements_raw;


-- Colocacoes agregadas por dia e por row do Discover.
-- `position` e a posicao DENTRO da row; menor e melhor.
CREATE OR REPLACE VIEW placements_daily AS
SELECT
    p.island_code,
    CAST(p.window_start AS DATE)        AS date,
    p.row_name,
    t.row_kind,
    MIN(p.position)                     AS best_position,
    MAX(p.position)                     AS worst_position,
    AVG(p.position)                     AS avg_position,
    SUM(p.hours)                        AS hours_in_row
FROM placements_raw p
LEFT JOIN row_taxonomy t USING (row_name)
GROUP BY 1, 2, 3, 4;


-- Uma linha por ilha e dia. `main_row` e a row de GENERO em que a ilha mais
-- ficou naquele dia - e a variavel dependente das perguntas causais, porque e
-- a unica cuja ordenacao a Epic afirma ser puramente algoritmica.
-- `on_editorial` marca os dias com Epic's Picks / Sponsored, que precisam ser
-- controlados ou excluidos.
CREATE OR REPLACE VIEW exposure_daily AS
WITH ranked AS (
    SELECT *, ROW_NUMBER() OVER (
               PARTITION BY island_code, date
               ORDER BY hours_in_row DESC, best_position ASC) AS rn_genre
    FROM placements_daily
    WHERE row_kind = 'genre'
),
totals AS (
    SELECT
        island_code, date,
        COUNT(*)                                                     AS rows_present,
        SUM(hours_in_row)                                            AS exposure_hours,
        MIN(best_position)                                           AS best_position_any_row,
        MAX(CASE WHEN row_kind = 'editorial' THEN 1 ELSE 0 END) = 1  AS on_editorial,
        SUM(CASE WHEN row_kind = 'editorial' THEN hours_in_row ELSE 0 END) AS editorial_hours
    FROM placements_daily
    GROUP BY 1, 2
)
SELECT
    t.island_code,
    t.date,
    t.rows_present,
    t.exposure_hours,
    t.best_position_any_row,
    t.on_editorial,
    t.editorial_hours,
    MAX(CASE WHEN r.rn_genre = 1 THEN r.row_name END)       AS main_row,
    MAX(CASE WHEN r.rn_genre = 1 THEN r.best_position END)  AS main_row_best_position,
    MAX(CASE WHEN r.rn_genre = 1 THEN r.worst_position END) AS main_row_worst_position,
    MAX(CASE WHEN r.rn_genre = 1 THEN r.avg_position END)   AS main_row_avg_position
FROM totals t
LEFT JOIN ranked r ON r.island_code = t.island_code AND r.date = t.date AND r.rn_genre = 1
GROUP BY 1, 2, 3, 4, 5, 6, 7;


-- PAINEL PRINCIPAL: ilha x dia, com defasagens dentro de cada ilha.
-- `d_` = variacao em relacao ao dia anterior DA MESMA ILHA (efeito fixo implicito).
-- Posicao menor e melhor, entao improve_position = posicao de ontem menos a de hoje:
-- positivo significa que subiu.
CREATE OR REPLACE VIEW panel AS
WITH base AS (
    SELECT
        COALESCE(e.island_code, p.island_code)   AS island_code,
        COALESCE(e.date, p.date)                 AS date,
        e.main_row,
        e.main_row_best_position                 AS position,
        e.main_row_worst_position                AS position_worst,
        e.main_row_avg_position                  AS position_avg,
        e.best_position_any_row,
        e.on_editorial,
        e.editorial_hours,
        e.exposure_hours,
        e.rows_present,
        p.ccu,
        m.unique_players,
        m.plays,
        m.minutes_played,
        m.avg_minutes_per_player,
        m.favorites,
        m.recommendations,
        m.retention_d1,
        m.retention_d7,
        -- derivados que a Epic nao entrega prontos
        CASE WHEN m.unique_players > 0
             THEN m.plays * 1.0 / m.unique_players END      AS sessions_per_player,
        CASE WHEN m.unique_players > 0
             THEN m.favorites * 1.0 / m.unique_players END   AS favorite_rate,
        CASE WHEN m.unique_players > 0
             THEN m.recommendations * 1.0 / m.unique_players END AS recommend_rate
    -- `date` chega como VARCHAR de players_daily/metrics e como DATE de
    -- exposure_daily; normalizamos tudo para DATE antes de juntar.
    FROM exposure_daily e
    FULL OUTER JOIN (SELECT island_code, CAST(date AS DATE) AS date, ccu
                     FROM players_daily) p
           ON p.island_code = e.island_code AND p.date = e.date
    LEFT JOIN (SELECT *, CAST(metric_date AS DATE) AS metric_day FROM metrics) m
           ON m.island_code = COALESCE(e.island_code, p.island_code)
          AND m.metric_day = COALESCE(e.date, p.date)
)
SELECT
    b.*,
    i.title_epic,
    i.creator_code_epic,
    i.tags,
    LAG(position)        OVER w AS position_prev,
    LAG(ccu)             OVER w AS ccu_prev,
    LAG(unique_players)  OVER w AS unique_players_prev,
    LAG(favorites)       OVER w AS favorites_prev,
    LAG(exposure_hours)  OVER w AS exposure_hours_prev,
    LAG(position) OVER w - position                       AS improve_position,
    ccu            - LAG(ccu) OVER w                      AS d_ccu,
    unique_players - LAG(unique_players) OVER w           AS d_unique_players,
    favorites      - LAG(favorites) OVER w                AS d_favorites,
    exposure_hours - LAG(exposure_hours) OVER w           AS d_exposure_hours,
    retention_d1   - LAG(retention_d1) OVER w             AS d_retention_d1
FROM base b
LEFT JOIN islands i USING (island_code)
WINDOW w AS (PARTITION BY b.island_code ORDER BY b.date);
