
const { capitalize, reverse, isPalindrome } = require('./utils');

test('capitalize empty string', () => {
    expect(capitalize('')).toBe('');
});

test('capitalize normal string', () => {
    expect(capitalize('hello')).toBe('Hello');
});

test('reverse string', () => {
    expect(reverse('hello')).toBe('olleh');
});

test('isPalindrome basic', () => {
    expect(isPalindrome('racecar')).toBe(true);
    expect(isPalindrome('hello')).toBe(false);
});
