-- -- brands for the specified content creators with sponsorship confidence >= 85, selecting the best post per brand for each creator

WITH best_per_pair AS (
  SELECT DISTINCT ON (ccr.id, tcbp.brand_raw_id)
    ccr.id AS creator_id, ccr.username AS creator_username, ccr.niche,
    tcbp.brand_name, tcbp.post_url, tcbp.sponsorship_confidence
  FROM content_creator_re ccr
  JOIN test_creator_brand_partnership_posts tcbp ON tcbp.content_creator_re_id = ccr.id
  WHERE ccr.id BETWEEN 43 AND 70
    AND tcbp.sponsorship_confidence >= 85
  ORDER BY ccr.id, tcbp.brand_raw_id, tcbp.sponsorship_confidence DESC NULLS LAST
)
SELECT creator_username, niche, brand_name, post_url, sponsorship_confidence
FROM best_per_pair
ORDER BY creator_id, sponsorship_confidence DESC;



///////////////////////////


  WITH best_per_pair AS (
    SELECT DISTINCT ON (ccr.id, tcbp.brand_raw_id)
      ccr.id AS creator_id,
      ccr.username AS creator_username,
      ccr.niche,
      tcbp.brand_name,
      tcbp.post_url,
      tcbp.sponsorship_confidence
    FROM content_creator_re ccr
    JOIN test_creator_brand_partnership_posts tcbp
      ON tcbp.content_creator_re_id = ccr.id
    WHERE ccr.id BETWEEN 43 AND 70
      AND tcbp.sponsorship_confidence >= 85
    ORDER BY
      ccr.id,
      tcbp.brand_raw_id,
      tcbp.sponsorship_confidence DESC NULLS LAST
  )
  SELECT
    creator_username,
    niche,
    brand_name,
    post_url,
    sponsorship_confidence
  FROM best_per_pair
  ORDER BY niche ASC, creator_username ASC, sponsorship_confidence DESC;




\\\\\\\\\\\\\\\\ same but add contidion referal = false for brands 

WITH best_per_pair AS (
  SELECT DISTINCT ON (ccr.id, tcbp.brand_raw_id)
    ccr.id AS creator_id,
    ccr.username AS creator_username,
    ccr.niche,
    tcbp.brand_name,
    tcbp.post_url,
    tcbp.sponsorship_confidence
  FROM content_creator_re ccr
  JOIN test_creator_brand_partnership_posts tcbp
    ON tcbp.content_creator_re_id = ccr.id
  JOIN brands_raw br
    ON br.id = tcbp.brand_raw_id
  WHERE ccr.id BETWEEN 43 AND 70
    AND tcbp.sponsorship_confidence >= 85
    AND br.refferls = false
  ORDER BY
    ccr.id,
    tcbp.brand_raw_id,
    tcbp.sponsorship_confidence DESC NULLS LAST
)
SELECT
  creator_username,
  niche,
  brand_name,
  post_url,
  sponsorship_confidence
FROM best_per_pair
ORDER BY
  niche ASC,
  creator_username ASC,
  sponsorship_confidence DESC;
  

\\\\\\\\\\\\\\\\\ distrinct brands for distinct creators in 2026 with refferls = false and sponsorship confidence >= 85 for the specified content creators

WITH best_per_pair AS (
  SELECT DISTINCT ON (ccr.id, tcbp.brand_raw_id)
    ccr.id AS creator_id,
    ccr.username AS creator_username,
    ccr.niche,
    tcbp.brand_name,
    tcbp.post_url,
    tcbp.post_timestamp,
    tcbp.sponsorship_confidence
  FROM content_creator_re ccr
  JOIN test_creator_brand_partnership_posts tcbp
    ON tcbp.content_creator_re_id = ccr.id
  JOIN brands_raw br
    ON br.id = tcbp.brand_raw_id
  WHERE ccr.id BETWEEN 71 AND 111
    AND tcbp.sponsorship_confidence >= 85
    AND br.refferls = false
    AND tcbp.post_timestamp >= '2026-01-01'
    AND tcbp.post_timestamp < '2027-01-01'
  ORDER BY
    ccr.id,
    tcbp.brand_raw_id,
    tcbp.sponsorship_confidence DESC NULLS LAST
)
SELECT
  creator_username,
  niche,
  brand_name,
  post_url,
  post_timestamp,
  sponsorship_confidence
FROM best_per_pair
ORDER BY
  niche ASC,
  creator_username ASC,
  sponsorship_confidence DESC;




\\\\\\\\\\\ count for upper query by niche music, fitness, beauty

WITH best_per_pair AS (
  SELECT DISTINCT ON (ccr.id, tcbp.brand_raw_id)
    ccr.id AS creator_id,
    ccr.username AS creator_username,
    ccr.niche,
    tcbp.brand_name,
    tcbp.post_url,
    tcbp.post_timestamp,
    tcbp.sponsorship_confidence
  FROM content_creator_re ccr
  JOIN test_creator_brand_partnership_posts tcbp
    ON tcbp.content_creator_re_id = ccr.id
  JOIN brands_raw br
    ON br.id = tcbp.brand_raw_id
  WHERE ccr.id BETWEEN 71 AND 111
    AND tcbp.sponsorship_confidence >= 85
    AND br.refferls = false
    AND tcbp.post_timestamp >= '2026-01-01'
    AND tcbp.post_timestamp < '2027-01-01'
  ORDER BY
    ccr.id,
    tcbp.brand_raw_id,
    tcbp.sponsorship_confidence DESC NULLS LAST
)
SELECT
  niche,
  COUNT(*) AS partnership_count
FROM best_per_pair
WHERE niche IN ('Music', 'Fitness', 'Beauty')
GROUP BY niche
ORDER BY partnership_count DESC;




\\ OR only distinct brands

WITH best_per_brand AS (
  SELECT DISTINCT ON (tcbp.brand_raw_id)
    ccr.username AS creator_username,
    ccr.niche,
    tcbp.brand_name,
    tcbp.post_url,
    tcbp.post_timestamp,
    tcbp.sponsorship_confidence
  FROM content_creator_re ccr
  JOIN test_creator_brand_partnership_posts tcbp
    ON tcbp.content_creator_re_id = ccr.id
  JOIN brands_raw br
    ON br.id = tcbp.brand_raw_id
  WHERE ccr.id BETWEEN 128 AND 168
    AND tcbp.sponsorship_confidence >= 85
    AND br.refferls = false
    AND tcbp.post_timestamp >= '2026-01-01'
    AND tcbp.post_timestamp < '2027-01-01'
  ORDER BY
    tcbp.brand_raw_id,
    tcbp.sponsorship_confidence DESC NULLS LAST,
    tcbp.post_timestamp DESC NULLS LAST
)
SELECT
  creator_username,
  niche,
  brand_name,
  post_url,
  post_timestamp,
  sponsorship_confidence
FROM best_per_brand
ORDER BY
  niche ASC,
  brand_name ASC;


-- count
WITH best_per_brand AS (
  SELECT DISTINCT ON (tcbp.brand_raw_id)
    ccr.username AS creator_username,
    ccr.niche,
    tcbp.brand_name,
    tcbp.post_url,
    tcbp.post_timestamp,
    tcbp.sponsorship_confidence
  FROM content_creator_re ccr
  JOIN test_creator_brand_partnership_posts tcbp
    ON tcbp.content_creator_re_id = ccr.id
  JOIN brands_raw br
    ON br.id = tcbp.brand_raw_id
  WHERE ccr.id BETWEEN 0 AND 168
    AND tcbp.sponsorship_confidence >= 85
    AND br.refferls = false
    AND tcbp.post_timestamp >= '2026-01-01'
    AND tcbp.post_timestamp < '2027-01-01'
  ORDER BY
    tcbp.brand_raw_id,
    tcbp.sponsorship_confidence DESC NULLS LAST,
    tcbp.post_timestamp DESC NULLS LAST
)
SELECT
  niche,
  COUNT(*) AS distinct_brand_count
FROM best_per_brand
WHERE niche IN ('Music', 'Beauty', 'Fitness', 'Health')
GROUP BY niche
ORDER BY distinct_brand_count DESC;


  -[ RECORD 1 ]-----+--------
niche             | Beauty
partnership_count | 47
-[ RECORD 2 ]-----+--------
niche             | Fitness
partnership_count | 19
-[ RECORD 3 ]-----+--------
niche             | Music
partnership_count | 18

