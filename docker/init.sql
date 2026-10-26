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
    (i % 4) + 4, 
    (random() * 1000 + 10)::numeric(10,2)
FROM generate_series(1, 100) i;

-- Insert 50 Employees
INSERT INTO employees (region_id, first_name, last_name, hire_date)
SELECT 
    (i % 3) + 1, 'First' || i, 'Last' || i, CURRENT_DATE - (i * 10 || ' days')::interval
FROM generate_series(1, 50) i;

-- Insert 10,000 Customers
INSERT INTO customers (region_id, name, segment, is_deleted)
SELECT 
    (i % 3) + 1, 
    'Customer ' || i,
    CASE (i % 3) WHEN 0 THEN 'enterprise' WHEN 1 THEN 'smb' ELSE 'consumer' END,
    CASE WHEN i % 50 = 0 THEN TRUE ELSE FALSE END -- 2% soft deleted
FROM generate_series(1, 10000) i;

-- Insert 50,000 Orders
INSERT INTO orders (customer_id, employee_id, status, order_date, gross_revenue, net_revenue)
SELECT 
    (i % 10000) + 1,
    (i % 50) + 1,
    CASE (i % 10) WHEN 0 THEN 'pending' WHEN 1 THEN 'cancelled' WHEN 2 THEN 'returned' ELSE 'completed' END,
    CURRENT_TIMESTAMP - (i * 1 || ' hours')::interval,
    (random() * 5000 + 100)::numeric(12,2),
    (random() * 4500 + 90)::numeric(12,2) -- Net is lower than gross
FROM generate_series(1, 50000) i;

-- Insert 150,000 Order Items (1 to 3 items per order)
INSERT INTO order_items (order_id, product_id, quantity, discount_pct, line_total)
SELECT 
    o.id,
    (o.id % 100) + 1,
    (random() * 5 + 1)::int,
    CASE WHEN random() > 0.8 THEN (random() * 0.2)::numeric(5,2) ELSE 0.00 END,
    (random() * 1000 + 10)::numeric(12,2)
FROM orders o
CROSS JOIN generate_series(1, 3) -- Up to 3 items per order
WHERE o.status != 'cancelled';

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
