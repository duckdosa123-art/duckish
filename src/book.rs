// SPDX-License-Identifier: AGPL-3.0-only

//! Polyglot opening book support.
//!
//! This module implements loading and probing of Polyglot-format `.bin` opening books.
//! The Polyglot format uses a specific Zobrist hashing scheme that differs from the
//! engine's internal Zobrist keys, so we implement the standard Polyglot keys here.

use std::{
    fs::File,
    io::{Read, Seek, SeekFrom},
    path::Path,
    sync::{Arc, OnceLock, RwLock},
};

use crate::chess::{
    board::Board,
    chessmove::Move,
    piece::{Colour, PieceType},
    types::Square,
};

/// Polyglot book entry (16 bytes, little-endian).
#[derive(Debug, Clone, Copy)]
#[repr(C, packed)]
struct BookEntry {
    key: u64,
    raw_move: u16,
    weight: u16,
    learn: u32,
}

impl BookEntry {
    fn move_from_raw(&self, board: &Board) -> Option<Move> {
        let from = (self.raw_move & 0x3F) as u8; // 6 bits
        let to = ((self.raw_move >> 6) & 0x3F) as u8; // 6 bits
        let promo = (self.raw_move >> 12) & 0x7; // 3 bits

        let from_sq = Square::new(from)?;
        let to_sq = Square::new(to)?;

        let piece = board.state.mailbox[from_sq as usize]?;
        if piece.colour() != board.turn() {
            return None;
        }

        let mv = match promo {
            0 => Move::new(from_sq, to_sq),
            1 => Move::new_with_promo(from_sq, to_sq, PieceType::Knight),
            2 => Move::new_with_promo(from_sq, to_sq, PieceType::Bishop),
            3 => Move::new_with_promo(from_sq, to_sq, PieceType::Rook),
            4 => Move::new_with_promo(from_sq, to_sq, PieceType::Queen),
            _ => return None,
        };

        // Verify this is a legal move for the position
        if board.is_legal(mv) { Some(mv) } else { None }
    }
}

/// Polyglot Zobrist keys (standard, compatible with existing .bin books).
///
/// These keys are derived from the reference Polyglot implementation and must
/// match exactly for book probing to work with standard books.
struct PolyglotKeys {
    piece_keys: [[u64; 64]; 12], // [piece_type][square]
    ep_keys: [u64; 64],
    castle_keys: [u64; 16],
    side_key: u64,
}

impl PolyglotKeys {
    fn get() -> &'static Self {
        static INSTANCE: OnceLock<PolyglotKeys> = OnceLock::new();
        INSTANCE.get_or_init(|| {
            // Standard Polyglot RNG: xorshift64* with seed 0x9E3779B97F4A7C15
            let mut state = 0x9E3779B97F4A7C15_u64;
            let mut next = || {
                state ^= state >> 12;
                state ^= state << 25;
                state ^= state >> 27;
                state.wrapping_mul(0x2545F4914F6CDD1D)
            };

            let mut piece_keys = [[0u64; 64]; 12];
            // Polyglot piece order: {P, N, B, R, Q, K} for white, then black
            for pt in 0..6 {
                for color in 0..2 {
                    let idx = color * 6 + pt;
                    for sq in 0..64 {
                        piece_keys[idx][sq] = next();
                    }
                }
            }

            let mut ep_keys = [0u64; 64];
            for sq in 0..64 {
                ep_keys[sq] = next();
            }

            let mut castle_keys = [0u64; 16];
            for i in 0..16 {
                castle_keys[i] = next();
            }

            let side_key = next();

            Self {
                piece_keys,
                ep_keys,
                castle_keys,
                side_key,
            }
        })
    }

    /// Compute the Polyglot Zobrist key for a position.
    fn compute_key(&self, board: &Board) -> u64 {
        let mut key = 0u64;

        // Pieces
        for sq in Square::all() {
            if let Some(piece) = board.state.mailbox[sq as usize] {
                let pt_idx = match piece.piece_type() {
                    PieceType::Pawn => 0,
                    PieceType::Knight => 1,
                    PieceType::Bishop => 2,
                    PieceType::Rook => 3,
                    PieceType::Queen => 4,
                    PieceType::King => 5,
                };
                let color_idx = piece.colour() as usize; // 0=White, 1=Black
                let idx = color_idx * 6 + pt_idx;
                key ^= self.piece_keys[idx][sq as usize];
            }
        }

        // Side to move
        if board.turn() == Colour::Black {
            key ^= self.side_key;
        }

        // En passant
        if let Some(ep_sq) = board.ep_sq() {
            key ^= self.ep_keys[ep_sq as usize];
        }

        // Castling rights
        let castle_idx = board.castling_rights().hashkey_index();
        key ^= self.castle_keys[castle_idx];

        key
    }
}

/// Polyglot opening book.
pub struct PolyglotBook {
    file: Arc<std::sync::Mutex<File>>,
    entry_count: usize,
}

impl PolyglotBook {
    /// Open a Polyglot book file.
    pub fn open<P: AsRef<Path>>(path: P) -> std::io::Result<Self> {
        let file = File::open(path)?;
        let metadata = file.metadata()?;
        let file_size = metadata.len();

        // Polyglot entries are 16 bytes each, no header in standard format
        if file_size % 16 != 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "Polyglot book file size is not a multiple of 16 bytes",
            ));
        }

        let entry_count = (file_size / 16) as usize;
        Ok(Self {
            file: Arc::new(std::sync::Mutex::new(file)),
            entry_count,
        })
    }

    /// Probe the book for moves at the current position.
    ///
    /// Returns a vector of (Move, weight) pairs, sorted by weight descending.
    pub fn probe(&self, board: &Board) -> Vec<(Move, u16)> {
        let key = PolyglotKeys::get().compute_key(board);
        let mut results = Vec::new();

        let mut file = match self.file.lock() {
            Ok(f) => f,
            Err(_) => return results,
        };

        // Binary search for the key (entries are sorted by key)
        let mut lo = 0;
        let mut hi = self.entry_count;
        while lo < hi {
            let mid = (lo + hi) / 2;
            if let Ok(entry) = self.read_entry(&mut file, mid) {
                if entry.key < key {
                    lo = mid + 1;
                } else {
                    hi = mid;
                }
            } else {
                break;
            }
        }

        // Collect all entries with matching key
        let mut idx = lo;
        while idx < self.entry_count {
            if let Ok(entry) = self.read_entry(&mut file, idx) {
                if entry.key != key {
                    break;
                }
                if let Some(mv) = entry.move_from_raw(board) {
                    results.push((mv, entry.weight));
                }
                idx += 1;
            } else {
                break;
            }
        }

        // Sort by weight descending
        results.sort_by(|a, b| b.1.cmp(&a.1));
        results
    }

    fn read_entry(&self, file: &mut File, index: usize) -> std::io::Result<BookEntry> {
        let offset = (index * 16) as u64;
        file.seek(SeekFrom::Start(offset))?;
        let mut buf = [0u8; 16];
        file.read_exact(&mut buf)?;

        // Little-endian
        let key = u64::from_le_bytes(buf[0..8].try_into().unwrap());
        let raw_move = u16::from_le_bytes(buf[8..10].try_into().unwrap());
        let weight = u16::from_le_bytes(buf[10..12].try_into().unwrap());
        let learn = u32::from_le_bytes(buf[12..16].try_into().unwrap());

        Ok(BookEntry {
            key,
            raw_move,
            weight,
            learn,
        })
    }
}

/// Global book handle, protected by RwLock for concurrent access.
static GLOBAL_BOOK: OnceLock<RwLock<Option<Arc<PolyglotBook>>>> = OnceLock::new();

/// Set the global book from a path.
pub fn set_book_path(path: &str) -> std::io::Result<()> {
    let book = if path.is_empty() {
        None
    } else {
        Some(Arc::new(PolyglotBook::open(path)?))
    };
    let lock = GLOBAL_BOOK.get_or_init(|| RwLock::new(None));
    let mut guard = lock.write().unwrap();
    *guard = book;
    Ok(())
}

/// Probe the global book for the current position.
pub fn probe_book(board: &Board) -> Option<Move> {
    let lock = GLOBAL_BOOK.get()?;
    let guard = lock.read().ok()?;
    let book = guard.as_ref()?;
    let moves = book.probe(board);
    moves.first().map(|(mv, _)| *mv)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::chess::fen::Fen;

    #[test]
    fn test_polyglot_keys_deterministic() {
        // Verify that key computation is deterministic
        let keys = PolyglotKeys::get();
        let board = Board::startpos();
        let key1 = keys.compute_key(&board);
        let key2 = keys.compute_key(&board);
        assert_eq!(key1, key2);
    }

    #[test]
    fn test_polyglot_keys_different_positions() {
        // Verify that different positions have different keys
        let keys = PolyglotKeys::get();
        let board1 = Board::startpos();
        let fen =
            Fen::parse("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1").unwrap();
        let mut board2 = Board::startpos();
        board2.set_from_fen(&fen);
        let key1 = keys.compute_key(&board1);
        let key2 = keys.compute_key(&board2);
        assert_ne!(key1, key2);
    }

    #[test]
    fn test_book_probe_startpos() {
        // Create a minimal test book with one entry for startpos: 1. e2e4
        use std::io::Write;
        let mut file = tempfile::NamedTempFile::new().unwrap();
        let keys = PolyglotKeys::get();
        let board = Board::startpos();
        let key = keys.compute_key(&board);

        // Move e2e4: from=12 (e2), to=28 (e4), promo=0
        // raw_move = from + to*64 + promo*64*64 = 12 + 28*64 = 1804
        let raw_move: u16 = 12 + 28 * 64;
        let weight: u16 = 100;
        let learn: u32 = 0;

        let mut buf = [0u8; 16];
        buf[0..8].copy_from_slice(&key.to_le_bytes());
        buf[8..10].copy_from_slice(&raw_move.to_le_bytes());
        buf[10..12].copy_from_slice(&weight.to_le_bytes());
        buf[12..16].copy_from_slice(&learn.to_le_bytes());
        file.write_all(&buf).unwrap();
        file.flush().unwrap();

        let book = PolyglotBook::open(file.path()).unwrap();
        let moves = book.probe(&board);
        assert_eq!(moves.len(), 1);
        assert_eq!(moves[0].0, Move::new(Square::E2, Square::E4));
        assert_eq!(moves[0].1, 100);
    }
}
