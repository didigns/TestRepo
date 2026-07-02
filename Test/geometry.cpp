// geometry.cpp - 2D 도형 넓이/둘레 계산 (다형성 예제)
#include <iostream>
#include <vector>
#include <memory>
#include <cmath>

class Shape {
public:
    virtual ~Shape() = default;
    virtual double area() const = 0;
    virtual double perimeter() const = 0;
    virtual std::string name() const = 0;
};

class Circle : public Shape {
    double r;
public:
    explicit Circle(double radius) : r(radius) {}
    double area() const override { return M_PI * r * r; }
    double perimeter() const override { return 2 * M_PI * r; }
    std::string name() const override { return "원"; }
};

class Rectangle : public Shape {
    double w, h;
public:
    Rectangle(double width, double height) : w(width), h(height) {}
    double area() const override { return w * h; }
    double perimeter() const override { return 2 * (w + h); }
    std::string name() const override { return "직사각형"; }
};

int main() {
    std::vector<std::unique_ptr<Shape>> shapes;
    shapes.push_back(std::make_unique<Circle>(3.0));
    shapes.push_back(std::make_unique<Rectangle>(4.0, 5.0));

    for (const auto &s : shapes) {
        std::cout << s->name()
                  << " 넓이=" << s->area()
                  << " 둘레=" << s->perimeter() << "\n";
    }
    return 0;
}
