// server.go - 인메모리 할 일(todo) REST API 서버 (Go)
package main

import (
	"encoding/json"
	"log"
	"net/http"
	"sync"
)

type Todo struct {
	ID    int    `json:"id"`
	Title string `json:"title"`
	Done  bool   `json:"done"`
}

type Store struct {
	mu    sync.Mutex
	items map[int]Todo
	next  int
}

func NewStore() *Store {
	return &Store{items: make(map[int]Todo), next: 1}
}

func (s *Store) Add(title string) Todo {
	s.mu.Lock()
	defer s.mu.Unlock()
	t := Todo{ID: s.next, Title: title}
	s.items[s.next] = t
	s.next++
	return t
}

func main() {
	store := NewStore()
	store.Add("장보기")
	store.Add("보고서 작성")

	http.HandleFunc("/todos", func(w http.ResponseWriter, r *http.Request) {
		store.mu.Lock()
		defer store.mu.Unlock()
		list := make([]Todo, 0, len(store.items))
		for _, t := range store.items {
			list = append(list, t)
		}
		json.NewEncoder(w).Encode(list)
	})

	log.Println("서버 시작: :8090")
	log.Fatal(http.ListenAndServe(":8090", nil))
}
