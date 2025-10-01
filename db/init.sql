CREATE TABLE IF NOT EXISTS images (
  id SERIAL PRIMARY KEY,
  url TEXT NOT NULL,
  processed BOOLEAN DEFAULT FALSE
);

-- seed ~300 placeholder images from picsum
INSERT INTO images (url)
SELECT 'https://picsum.photos/seed/' || g || '/1024/768'
FROM generate_series(1, 300) g;