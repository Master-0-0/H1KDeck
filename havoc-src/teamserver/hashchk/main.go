package main

import (
	"encoding/hex"
	"fmt"

	"golang.org/x/crypto/sha3"
)

func main() {
	h := sha3.New256()
	h.Write([]byte("password1234"))
	fmt.Println(hex.EncodeToString(h.Sum(nil)))
}
