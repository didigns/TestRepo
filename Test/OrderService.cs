// OrderService.cs - 주문 처리 서비스 (C# 예제)
using System;
using System.Collections.Generic;
using System.Linq;

namespace ShopApp.Services
{
    public record OrderLine(string Product, int Quantity, decimal UnitPrice)
    {
        public decimal Subtotal => Quantity * UnitPrice;
    }

    public class Order
    {
        public Guid Id { get; } = Guid.NewGuid();
        public DateTime CreatedAt { get; } = DateTime.UtcNow;
        public List<OrderLine> Lines { get; } = new();

        public decimal Total => Lines.Sum(l => l.Subtotal);
    }

    public class OrderService
    {
        private readonly List<Order> _orders = new();

        public Order CreateOrder(IEnumerable<OrderLine> lines)
        {
            var order = new Order();
            order.Lines.AddRange(lines);
            _orders.Add(order);
            return order;
        }

        public decimal RevenueSince(DateTime from) =>
            _orders.Where(o => o.CreatedAt >= from).Sum(o => o.Total);

        public static void Main()
        {
            var svc = new OrderService();
            var order = svc.CreateOrder(new[]
            {
                new OrderLine("키보드", 2, 89000m),
                new OrderLine("모니터", 1, 320000m),
            });
            Console.WriteLine($"주문 {order.Id} 합계: {order.Total:N0}원");
        }
    }
}
