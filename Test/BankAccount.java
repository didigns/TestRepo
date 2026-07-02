// BankAccount.java - 간단한 은행 계좌 (입출금/이체) 예제
package com.example.bank;

import java.util.ArrayList;
import java.util.List;

public class BankAccount {
    private final String owner;
    private long balance;
    private final List<String> history = new ArrayList<>();

    public BankAccount(String owner, long initial) {
        this.owner = owner;
        this.balance = initial;
        history.add("개설: " + initial + "원");
    }

    public void deposit(long amount) {
        if (amount <= 0) throw new IllegalArgumentException("입금액은 0보다 커야 합니다.");
        balance += amount;
        history.add("입금: " + amount + "원");
    }

    public void withdraw(long amount) {
        if (amount > balance) throw new IllegalStateException("잔액 부족");
        balance -= amount;
        history.add("출금: " + amount + "원");
    }

    public void transfer(BankAccount to, long amount) {
        this.withdraw(amount);
        to.deposit(amount);
        history.add("이체 -> " + to.owner + ": " + amount + "원");
    }

    public long getBalance() { return balance; }

    public static void main(String[] args) {
        BankAccount a = new BankAccount("홍길동", 100000);
        BankAccount b = new BankAccount("김영희", 0);
        a.transfer(b, 30000);
        System.out.println("홍길동 잔액: " + a.getBalance());
        System.out.println("김영희 잔액: " + b.getBalance());
    }
}
