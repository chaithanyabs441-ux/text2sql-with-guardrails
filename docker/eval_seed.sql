-- Seed the ancillary tables used by evaluation queries.
-- Each table is populated only when empty, so this is safe to rerun.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM core.payments) THEN
        INSERT INTO core.payments (order_id, payment_method, amount, status, processed_at)
        SELECT
            o.id,
            CASE (o.id % 3)
                WHEN 0 THEN 'card'
                WHEN 1 THEN 'bank_transfer'
                ELSE 'digital_wallet'
            END,
            o.net_revenue,
            CASE
                WHEN o.status = 'returned' THEN 'refunded'
                WHEN o.status = 'cancelled' OR o.id % 20 = 0 THEN 'failed'
                ELSE 'success'
            END,
            o.order_date + INTERVAL '2 hours'
        FROM core.orders AS o
        WHERE o.net_revenue IS NOT NULL;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM core.returns) THEN
        INSERT INTO core.returns (order_item_id, reason, refund_amount, return_date)
        SELECT
            oi.id,
            CASE (oi.id % 4)
                WHEN 0 THEN 'damaged'
                WHEN 1 THEN 'incorrect_item'
                WHEN 2 THEN 'customer_changed_mind'
                ELSE 'other'
            END,
            oi.line_total,
            LEAST(CURRENT_DATE, o.order_date::date + 7)
        FROM core.order_items AS oi
        JOIN core.orders AS o ON o.id = oi.order_id
        WHERE o.status = 'returned' AND oi.id % 10 = 0;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM core.support_tickets) THEN
        INSERT INTO core.support_tickets (
            customer_id, issue_type, resolution_time_hours, created_at
        )
        SELECT
            (i % 10000) + 1,
            CASE (i % 5)
                WHEN 0 THEN 'billing'
                WHEN 1 THEN 'delivery'
                WHEN 2 THEN 'product_question'
                WHEN 3 THEN 'technical'
                ELSE 'return_request'
            END,
            CASE
                WHEN i % 7 = 0 THEN NULL
                ELSE ((i % 72) + 1)::numeric(6, 2)
            END,
            CURRENT_TIMESTAMP - (i % 1825) * INTERVAL '1 day'
        FROM generate_series(1, 5000) AS series(i);
    END IF;
END
$$;
