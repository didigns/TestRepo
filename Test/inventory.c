/*
 * inventory.c - 간단한 창고 재고 관리 프로그램
 * 품목 추가, 출고, 재고 현황 출력 기능을 제공한다.
 */
#include <stdio.h>
#include <string.h>

#define MAX_ITEMS 100

typedef struct {
    int   id;
    char  name[64];
    int   quantity;
    double price;
} Item;

static Item warehouse[MAX_ITEMS];
static int  item_count = 0;

int add_item(int id, const char *name, int qty, double price) {
    if (item_count >= MAX_ITEMS) return -1;
    Item *it = &warehouse[item_count++];
    it->id = id;
    strncpy(it->name, name, sizeof(it->name) - 1);
    it->quantity = qty;
    it->price = price;
    return item_count;
}

int ship_item(int id, int qty) {
    for (int i = 0; i < item_count; i++) {
        if (warehouse[i].id == id) {
            if (warehouse[i].quantity < qty) return -1; /* 재고 부족 */
            warehouse[i].quantity -= qty;
            return warehouse[i].quantity;
        }
    }
    return -1; /* 품목 없음 */
}

double total_value(void) {
    double sum = 0.0;
    for (int i = 0; i < item_count; i++)
        sum += warehouse[i].quantity * warehouse[i].price;
    return sum;
}

int main(void) {
    add_item(1001, "노트북", 15, 1200000.0);
    add_item(1002, "무선마우스", 80, 25000.0);
    add_item(1003, "USB-C 허브", 40, 45000.0);

    ship_item(1001, 3);
    ship_item(1002, 20);

    printf("총 재고 자산가치: %.0f 원\n", total_value());
    return 0;
}
