use crucible_core::{channel_of, identify_gtnh_name, parse_pack_version};
use std::hint::black_box;
use std::time::Instant;

const ROUNDS: usize = 15;
const ITERATIONS_PER_ROUND: usize = 2_000;
const WARMUP_ITERATIONS: usize = 100;

fn fixture_inputs(source: &str) -> Vec<String> {
    source
        .lines()
        .filter(|line| !line.is_empty() && !line.starts_with('#'))
        .filter_map(|line| line.split_once('\t').map(|(input, _)| input.to_owned()))
        .collect()
}

fn percentile(sorted: &[u128], numerator: usize, denominator: usize) -> u128 {
    let index = (sorted.len() * numerator).div_ceil(denominator).saturating_sub(1);
    sorted[index.min(sorted.len() - 1)]
}

fn measure<T, F>(label: &str, inputs: &[String], mut parse: F)
where
    F: FnMut(&str) -> T,
{
    for _ in 0..WARMUP_ITERATIONS {
        for input in inputs {
            black_box(parse(black_box(input)));
        }
    }

    let mut samples = Vec::with_capacity(ROUNDS);
    for _ in 0..ROUNDS {
        let started = Instant::now();
        for _ in 0..ITERATIONS_PER_ROUND {
            for input in inputs {
                black_box(parse(black_box(input)));
            }
        }
        let operations = (ITERATIONS_PER_ROUND * inputs.len()) as u128;
        samples.push(started.elapsed().as_nanos() / operations);
    }
    samples.sort_unstable();

    println!(
        "{label}: n={} p50={} ns/op p95={} ns/op rounds={} iterations/round={}",
        inputs.len(),
        percentile(&samples, 50, 100),
        percentile(&samples, 95, 100),
        ROUNDS,
        ITERATIONS_PER_ROUND
    );
}

fn main() {
    let versions = fixture_inputs(include_str!("../fixtures/pack-version-cases.tsv"));
    let names = fixture_inputs(include_str!("../fixtures/pack-name-cases.tsv"));
    assert!(!versions.is_empty(), "version fixtures must not be empty");
    assert!(!names.is_empty(), "name fixtures must not be empty");

    println!("Crucible parser microbenchmark; Rust build profile supplied by cargo bench");
    measure("parse_pack_version", &versions, parse_pack_version);
    measure("channel_of", &versions, channel_of);
    measure("identify_gtnh_name", &names, identify_gtnh_name);
}
