-- schema.sql - 온라인 서점 주문 데이터베이스 스키마 (PostgreSQL)

CREATE TABLE customers (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,
    email       VARCHAR(255) UNIQUE NOT NULL,
    created_at  TIMESTAMP DEFAULT now()
);

CREATE TABLE books (
    id       SERIAL PRIMARY KEY,
    title    VARCHAR(300) NOT NULL,
    author   VARCHAR(150),
    price    NUMERIC(10,2) NOT NULL,
    stock    INTEGER DEFAULT 0
);

CREATE TABLE orders (
    id           SERIAL PRIMARY KEY,
    customer_id  INTEGER REFERENCES customers(id),
    ordered_at   TIMESTAMP DEFAULT now(),
    status       VARCHAR(20) DEFAULT 'pending'
);

CREATE TABLE order_items (
    order_id   INTEGER REFERENCES orders(id),
    book_id    INTEGER REFERENCES books(id),
    quantity   INTEGER NOT NULL,
    PRIMARY KEY (order_id, book_id)
);

-- 월별 매출 집계 뷰
CREATE VIEW monthly_revenue AS
SELECT date_trunc('month', o.ordered_at) AS month,
       SUM(oi.quantity * b.price)        AS revenue
FROM orders o
JOIN order_items oi ON oi.order_id = o.id
JOIN books b        ON b.id = oi.book_id
WHERE o.status = 'paid'
GROUP BY 1
ORDER BY 1;
