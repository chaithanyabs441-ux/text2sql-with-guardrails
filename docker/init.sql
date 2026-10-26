-- Create schemas
CREATE SCHEMA IF NOT EXISTS core;
SET search_path TO core;

-- 1. Regions
CREATE TABLE regions (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL,
    code VARCHAR(10) UNIQUE NOT NULL
);

-- 2. Categories
CREATE TABLE categories (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL,
    parent_id INT REFERENCES categories(id)
);

-- 3. Products
CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    category_id INT REFERENCES categories(id),
    unit_price NUMERIC(10, 2) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE
);

-- 4. Customers
CREATE TABLE customers (
    id SERIAL PRIMARY KEY,
    region_id INT REFERENCES regions(id),
    name VARCHAR(100) NOT NULL,
    segment VARCHAR(20) CHECK (segment IN ('enterprise', 'smb', 'consumer')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT FALSE -- Soft delete
);

-- 5. Employees
CREATE TABLE employees (
    id SERIAL PRIMARY KEY,
    region_id INT REFERENCES regions(id),
    first_name VARCHAR(50),
    last_name VARCHAR(50),
    hire_date DATE
);

-- 6. Orders
CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    customer_id INT REFERENCES customers(id),
    employee_id INT REFERENCES employees(id),
    status VARCHAR(20) CHECK (status IN ('completed', 'pending', 'cancelled', 'returned')),
    order_date TIMESTAMP NOT NULL,
    gross_revenue NUMERIC(12, 2), -- Tricky: gross vs net
    net_revenue NUMERIC(12, 2),   -- Tricky: gross vs net
    shipping_address TEXT
);

-- 7. Order Items
CREATE TABLE order_items (
    id SERIAL PRIMARY KEY,
    order_id INT REFERENCES orders(id),
    product_id INT REFERENCES products(id),
    quantity INT NOT NULL,
    discount_pct NUMERIC(5, 2) DEFAULT 0.00, -- NULL-heavy column often
    line_total NUMERIC(12, 2) NOT NULL
);

-- 8. Payments
CREATE TABLE payments (
    id SERIAL PRIMARY KEY,
    order_id INT REFERENCES orders(id),
    payment_method VARCHAR(50),
    amount NUMERIC(12, 2) NOT NULL,
    status VARCHAR(20) CHECK (status IN ('success', 'failed', 'refunded')),
    processed_at TIMESTAMP
);

-- 9. Returns
CREATE TABLE returns (
    id SERIAL PRIMARY KEY,
    order_item_id INT REFERENCES order_items(id),
    reason VARCHAR(255),
    refund_amount NUMERIC(12, 2),
    return_date DATE
);

-- 10. Support Tickets
CREATE TABLE support_tickets (
    id SERIAL PRIMARY KEY,
    customer_id INT REFERENCES customers(id),
    issue_type VARCHAR(50),
    resolution_time_hours NUMERIC(6, 2), -- NULL-heavy
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------
-- POPULATE DATA (Generating 100,000+ rows quickly)
-- ---------------------------------------------------------

INSERT INTO regions (name, code) VALUES 
('North America', 'NA'), ('Europe', 'EU'), ('Asia Pacific', 'APAC');

INSERT INTO categories (name) VALUES 
('Electronics'), ('Software'), ('Services');
INSERT INTO categories (name, parent_id) VALUES 
('Laptops', 1), ('Phones', 1), ('Cloud', 2), ('Consulting', 3);

-- Insert 100 Products
INSERT INTO products (name, category_id, unit_price)
SELECT 
    'Product ' || i, 
    CASE
        WHEN i <= 55 THEN 4
        WHEN i <= 80 THEN 5
        WHEN i <= 93 THEN 6
        ELSE 7
    END,
    (random() * 1000 + 10)::numeric(10,2)
FROM generate_series(1, 100) i;

-- Insert 50 Employees
INSERT INTO employees (region_id, first_name, last_name, hire_date)
SELECT 
    (i % 3) + 1,
    'First' || i,
    'Last' || i,
    CASE
        WHEN i % 10 = 0 THEN NULL
        ELSE CURRENT_DATE - ((i * 37) % 3650)
    END
FROM generate_series(1, 50) i;

-- Insert 10,000 Customers
INSERT INTO customers (region_id, name, segment, created_at, is_deleted)
SELECT 
    CASE
        WHEN i % 173 = 0 THEN NULL
        ELSE ((i * 17 + (i / 7) * 11) % 3) + 1
    END,
    'Customer ' || i,
    CASE
        WHEN i % 100 < 10 THEN 'enterprise'
        WHEN i % 100 < 40 THEN 'smb'
        ELSE 'consumer'
    END,
    CURRENT_TIMESTAMP - ((i * 29) % 1826) * INTERVAL '1 day',
    CASE WHEN i % 50 = 0 THEN TRUE ELSE FALSE END -- 2% soft deleted
FROM generate_series(1, 10000) i;

-- Insert 50,000 Orders
INSERT INTO orders (
    customer_id, employee_id, status, order_date, gross_revenue, net_revenue, shipping_address
)
SELECT 
    CASE WHEN i % 173 = 0 THEN NULL ELSE (i % 10000) + 1 END,
    CASE
        WHEN i % 100 < 30 THEN (i % 5) + 1
        WHEN i % 100 < 55 THEN (i % 10) + 6
        WHEN i % 100 < 75 THEN (i % 15) + 16
        WHEN i % 100 < 90 THEN (i % 15) + 31
        ELSE (i % 5) + 46
    END,
    CASE
        WHEN i % 100 < 55 THEN 'completed'
        WHEN i % 100 < 75 THEN 'pending'
        WHEN i % 100 < 90 THEN 'cancelled'
        ELSE 'returned'
    END,
    CURRENT_TIMESTAMP
        - ((i * 31) % 1826) * INTERVAL '1 day'
        - ((i * 17) % 24) * INTERVAL '1 hour',
    CASE
        WHEN i % 113 = 0 THEN NULL
        ELSE (random() * 5000 + 100)::numeric(12,2)
    END,
    CASE
        WHEN i % 127 = 0 THEN NULL
        ELSE (random() * 4500 + 90)::numeric(12,2)
    END,
    CASE
        WHEN i % 5 = 0 THEN 'Street ' || (i % 900 + 1) || ', Sample City'
        ELSE NULL
    END
FROM generate_series(1, 50000) i;

-- Insert up to 3 items per non-cancelled order, with skewed product demand.
INSERT INTO order_items (order_id, product_id, quantity, discount_pct, line_total)
SELECT 
    o.id,
    CASE
        WHEN ((o.id * 3 + item.item_no) % 100) < 60
            THEN ((o.id * 3 + item.item_no) % 10) + 1
        WHEN ((o.id * 3 + item.item_no) % 100) < 85
            THEN ((o.id * 11 + item.item_no * 7) % 20) + 11
        WHEN ((o.id * 3 + item.item_no) % 100) < 95
            THEN ((o.id * 13 + item.item_no) % 40) + 31
        ELSE ((o.id * 17 + item.item_no * 3) % 30) + 71
    END,
    (random() * 5 + 1)::int,
    CASE
        WHEN (o.id * 3 + item.item_no) % 17 = 0 THEN NULL
        WHEN random() > 0.8 THEN (random() * 0.2)::numeric(5,2)
        ELSE 0.00
    END,
    (random() * 1000 + 10)::numeric(12,2)
FROM orders o
CROSS JOIN generate_series(1, 3) AS item(item_no)
WHERE o.status <> 'cancelled'
  AND item.item_no <= CASE WHEN o.id % 10 = 0 THEN 1 WHEN o.id % 3 = 0 THEN 2 ELSE 3 END;

-- A separate application login with read-only access to business data.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 't2s_readonly'
    ) THEN
        EXECUTE 'CREATE ROLE t2s_readonly LOGIN PASSWORD ''t2s_readonly_password'' NOINHERIT';
    END IF;
END
$$;

ALTER ROLE t2s_readonly
    WITH LOGIN
    PASSWORD 't2s_readonly_password'
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION
    NOBYPASSRLS
    NOINHERIT;
ALTER ROLE t2s_readonly SET statement_timeout = '5s';
ALTER ROLE t2s_readonly SET default_transaction_read_only = on;

REVOKE ALL ON SCHEMA core FROM t2s_readonly;
GRANT USAGE ON SCHEMA core TO t2s_readonly;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA core FROM t2s_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA core TO t2s_readonly;

-- Preserve the same access policy for future core tables created by this role.
ALTER DEFAULT PRIVILEGES IN SCHEMA core
    REVOKE ALL ON TABLES FROM t2s_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA core
    GRANT SELECT ON TABLES TO t2s_readonly;
